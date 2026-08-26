"""Unit tests for OrchestratorRunner."""

from __future__ import annotations

from collections.abc import AsyncIterator
import copy
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.errors import ConfigError, PersistenceError
from ouroboros.core.project_identity import resolve_project_identity
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    BrownfieldContext,
    ContextReference,
    EvaluationPrinciple,
    ExitCondition,
    InvestmentSpec,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
    ac_texts,
)
from ouroboros.core.types import Result
from ouroboros.core.worktree import TaskWorkspace
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.coordinator import CoordinatorReview
from ouroboros.orchestrator.decomposition_limits import MAX_DECOMPOSITION_DEPTH
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

# TODO: uncomment when OpenCode runtime is shipped
# from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    CoordinatorQuotaPause,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
)
from ouroboros.orchestrator.profile_loader import SuggestedModelTier
from ouroboros.orchestrator.recoverable_failure import UsageLimitPauseConsequence
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    OrchestratorError,
    OrchestratorResult,
    OrchestratorRunner,
    _seed_has_investment_metadata,
    build_system_prompt,
    build_task_prompt,
    clear_cancellation,
    is_cancellation_requested,
    request_cancellation,
)
from ouroboros.orchestrator.runtime_error import classify_subprocess_failure
from ouroboros.orchestrator.session import (
    SESSION_START_IDENTITY_PROGRESS_KEY,
    SessionStatus,
    SessionTracker,
)
from ouroboros.orchestrator.worker_runtime import LeaderDrivenWorkerRuntime
from ouroboros.persistence.event_store import EventStore

_LONG_WINDOW_429_ENCODINGS = tuple(
    (field, value)
    for field in (
        "http_status",
        "httpStatus",
        "http_status_code",
        "httpStatusCode",
        "status_code",
        "statusCode",
        "api_error_status",
        "apiErrorStatus",
        "status",
        "code",
        "error_code",
        "errorCode",
    )
    for value in (429, "429")
)
_EXPECTED_CANONICAL_PROJECT_CWD = str(Path("/tmp/project").resolve())


def _task_workspace() -> TaskWorkspace:
    return TaskWorkspace(
        durable_id="orch_test",
        repo_root="/tmp/repo",
        repo_name="repo",
        original_cwd="/tmp/repo",
        effective_cwd="/tmp/worktree/repo/orch_test",
        worktree_path="/tmp/worktree/repo/orch_test",
        branch="ooo/orch_test",
        lock_path="/tmp/worktree/.locks/repo/orch_test.json",
    )


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Runner Test",
            "-c",
            "user.email=runner@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _runtime_owned_task_workspace(adapter: Any, root: Path) -> TaskWorkspace:
    repo_root = root / "repo"
    worktree_path = root / "worktree"
    _init_git_repo(repo_root)
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "ooo/orch_test", str(worktree_path), "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    workspace = TaskWorkspace(
        durable_id="orch_test",
        repo_root=str(repo_root),
        repo_name="repo",
        original_cwd=str(repo_root),
        effective_cwd=str(worktree_path),
        worktree_path=str(worktree_path),
        branch="ooo/orch_test",
        lock_path=str(root / ".locks" / "repo" / "orch_test.json"),
    )
    adapter.working_directory = workspace.effective_cwd
    return workspace


def _bare_linked_task_workspace(adapter: Any, root: Path) -> TaskWorkspace:
    seed = root / "seed"
    common_git = root / "common.git"
    source = root / "source"
    worktree = root / "worktree"
    _init_git_repo(seed)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(common_git)],
        check=True,
        capture_output=True,
    )
    for branch, checkout in (("source", source), ("execution", worktree)):
        subprocess.run(
            [
                "git",
                f"--git-dir={common_git}",
                "worktree",
                "add",
                "-q",
                "-b",
                branch,
                str(checkout),
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
    source_cwd = source / "packages" / "app"
    execution_cwd = worktree / "packages" / "app"
    source_cwd.mkdir(parents=True)
    execution_cwd.mkdir(parents=True)
    workspace = TaskWorkspace(
        durable_id="bare-linked",
        repo_root=str(source),
        repo_name="source",
        original_cwd=str(source_cwd),
        effective_cwd=str(execution_cwd),
        worktree_path=str(worktree),
        branch="execution",
        lock_path=str(root / ".locks" / "bare-linked.json"),
    )
    adapter.working_directory = workspace.effective_cwd
    return workspace


def _allow_mocked_precreated_durable_state(runner: OrchestratorRunner) -> None:
    """Treat a unit-test tracker as the durable snapshot for mocked stores."""

    async def reconstruct(tracker: SessionTracker):
        return Result.ok(tracker)

    runner._reconstruct_precreated_durable_tracker = AsyncMock(side_effect=reconstruct)


def _attach_live_process_local_contract(
    runner: OrchestratorRunner,
    tracker: SessionTracker,
    seed: Seed,
    *,
    session_id: str,
) -> SessionTracker:
    """Build the same-process capability required by resume-focused tests."""
    if not isinstance(getattr(runner._adapter, "llm_backend", None), str):
        runner._adapter.llm_backend = "test-llm"
    if not isinstance(getattr(runner._adapter, "_model", None), str):
        runner._adapter._model = "test-model"
    generation = runner._begin_process_local_authority_generation()
    contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=seed, authority_generation=generation
    )
    runner._register_process_local_authority(
        session_id=session_id,
        execution_id=tracker.execution_id,
        execution_contract=contract,
        generation=generation,
    )
    runner._seal_process_local_prepared_contract(
        session_id=session_id,
        execution_id=tracker.execution_id,
        generation=generation,
        execution_contract=contract,
    )
    _allow_mocked_precreated_durable_state(runner)
    return tracker.with_progress({EXECUTION_CONTRACT_PROGRESS_KEY: contract})


def _enable_direct_bounded_routes(
    runner: OrchestratorRunner,
    adapter: MagicMock,
) -> None:
    from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
    from ouroboros.orchestrator.model_routing import build_model_router

    economics = EconomicsConfig(  # type: ignore[arg-type]
        default_tier="frugal",
        escalation_threshold=2,
        tiers={
            "frugal": TierConfig(
                cost_factor=1,
                models=[ModelConfig(provider="anthropic", model="haiku-x")],
            ),
            "standard": TierConfig(
                cost_factor=10,
                models=[ModelConfig(provider="anthropic", model="sonnet-x")],
            ),
            "frontier": TierConfig(
                cost_factor=30,
                models=[ModelConfig(provider="anthropic", model="opus-x")],
            ),
        },
    )
    adapter.runtime_backend = "claude"
    adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=ParamSupport.NATIVE,
    )
    runner._route_economics = economics
    runner._model_router = build_model_router(economics, runtime_backend="claude")
    assert runner._model_router is not None
    _allow_mocked_precreated_durable_state(runner)


def test_seed_investment_detection_supports_legacy_string_criteria(sample_seed: Seed) -> None:
    assert _seed_has_investment_metadata(sample_seed) is False


def test_seed_investment_detection_finds_structured_metadata(sample_seed: Seed) -> None:
    seed = sample_seed.model_copy(
        update={
            "acceptance_criteria": (
                sample_seed.acceptance_criteria[0],
                AcceptanceCriterionSpec(
                    description="Protect the production migration",
                    investment=InvestmentSpec(stakes="high"),
                ),
            )
        }
    )

    assert _seed_has_investment_metadata(seed) is True


@pytest.fixture
def sample_seed() -> Seed:
    """Create a sample seed for testing."""
    return Seed(
        goal="Build a task management CLI",
        constraints=("Python 3.14+", "No external database"),
        acceptance_criteria=(
            "Tasks can be created",
            "Tasks can be listed",
            "Tasks can be deleted",
        ),
        ontology_schema=OntologySchema(
            name="TaskManager",
            description="Task management ontology",
            fields=(
                OntologyField(
                    name="tasks",
                    field_type="array",
                    description="List of tasks",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="completeness",
                description="All requirements are met",
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="all_criteria_met",
                description="All acceptance criteria satisfied",
                evaluation_criteria="100% criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.15),
    )


class TestBuildSystemPrompt:
    """Tests for build_system_prompt function."""

    def test_includes_goal(self, sample_seed: Seed) -> None:
        """Test that system prompt includes the goal."""
        prompt = build_system_prompt(sample_seed)
        assert sample_seed.goal in prompt

    def test_binds_dedicated_worker_role(self, sample_seed: Seed) -> None:
        """Background execution must run in this worker, not hand off again."""
        prompt = build_system_prompt(sample_seed)

        assert "## Ouroboros Execution Worker Role" in prompt
        assert "Execute the Seed directly in this worker process" in prompt
        assert "Do not delegate, hand off, or spawn another agent" in prompt

    def test_includes_constraints(self, sample_seed: Seed) -> None:
        """Test that system prompt includes constraints."""
        prompt = build_system_prompt(sample_seed)
        assert "Python 3.14+" in prompt
        assert "No external database" in prompt

    def test_includes_evaluation_principles(self, sample_seed: Seed) -> None:
        """Test that system prompt includes evaluation principles."""
        prompt = build_system_prompt(sample_seed)
        assert "completeness" in prompt
        assert "All requirements are met" in prompt
        assert "(weight" not in prompt

    def test_system_prompt_leaves_acceptance_criteria_to_task_prompt(
        self, sample_seed: Seed
    ) -> None:
        """System prompt should not duplicate task-level acceptance criteria."""
        system_prompt = build_system_prompt(sample_seed)
        task_prompt = build_task_prompt(sample_seed)

        assert "## Acceptance Criteria" not in system_prompt
        for criterion in ac_texts(sample_seed.acceptance_criteria):
            assert criterion not in system_prompt
            assert criterion in task_prompt

    def test_includes_seed_contract_ontology_lens(self, sample_seed: Seed) -> None:
        """System prompt renders Seed ontology as an execution contract lens."""
        prompt = build_system_prompt(sample_seed)

        assert "## Seed Contract" in prompt
        assert "## Ontology / Conceptual Lens" in prompt
        assert "conceptual lens for execution decisions" in prompt
        assert "It is not a mandatory output outline." in prompt
        assert "Name: TaskManager" in prompt
        assert "Description: Task management ontology" in prompt
        assert "- tasks [array]: List of tasks (required concept)" in prompt
        assert "closer to the Seed's intended outcome" in prompt
        assert "Do not force the final artifact to mirror these fields" in prompt

    def test_includes_self_recovery_protocol(self, sample_seed: Seed) -> None:
        """Run prompts should tell agents how to change strategy when stuck."""
        prompt = build_system_prompt(sample_seed)
        assert "Self-Recovery Protocol" in prompt
        assert "spinning" in prompt
        assert "no acceptance-criterion progress" in prompt

    def test_renders_bounded_successor_directive_after_seed_contract(
        self,
        sample_seed: Seed,
    ) -> None:
        seed = Seed.from_dict(
            {
                **sample_seed.to_dict(),
                "conductor_directive": {
                    "source_attention_event_id": "attention_1",
                    "instruction": "Add repository evidence for the rejected claim.",
                    "rejected_reasons": ["The cited file was not observed."],
                    "deterministic": True,
                },
            }
        )

        prompt = build_system_prompt(seed)

        assert prompt.index("## Seed Contract") < prompt.index(
            "## Active Conductor Successor Directive"
        )
        assert "The Seed above remains" in prompt
        assert "the source of truth" in prompt
        assert "Add repository evidence for the rejected claim." in prompt
        assert "- acceptance criteria: true" in prompt

    def test_handles_empty_constraints(self) -> None:
        """Test handling seed with no constraints."""
        seed = Seed(
            goal="Test goal",
            constraints=(),
            acceptance_criteria=("AC1",),
            ontology_schema=OntologySchema(
                name="Test",
                description="Test",
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )
        prompt = build_system_prompt(seed)
        assert "None" in prompt or "Constraints" in prompt

    def test_handles_empty_ontology_fields(self) -> None:
        """Ontology lens renders cleanly even when no concepts are listed."""
        seed = Seed(
            goal="Test goal",
            constraints=(),
            acceptance_criteria=("AC1",),
            ontology_schema=OntologySchema(
                name="TestOntology",
                description="A minimal ontology",
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

        prompt = build_system_prompt(seed)

        assert "## Ontology / Conceptual Lens" in prompt
        assert "Name: TestOntology" in prompt
        assert "Description: A minimal ontology" in prompt
        assert "Concepts:" not in prompt
        assert "When execution decisions are ambiguous:" in prompt

    def test_includes_brownfield_context(self, sample_seed: Seed) -> None:
        """System prompt preserves brownfield project references and constraints."""
        seed = sample_seed.model_copy(
            update={
                "brownfield_context": BrownfieldContext(
                    project_type="brownfield",
                    context_references=(
                        ContextReference(
                            path="/repo/app",
                            role="primary",
                            summary="Main application repository",
                        ),
                    ),
                    existing_patterns=("Use repository service classes",),
                    existing_dependencies=("sqlalchemy",),
                )
            }
        )

        prompt = build_system_prompt(seed)

        assert "## Existing Codebase Context (BROWNFIELD)" in prompt
        assert "[PRIMARY] /repo/app: Main application repository" in prompt
        assert "Use repository service classes" in prompt
        assert "sqlalchemy" in prompt


class TestBuildTaskPrompt:
    """Tests for build_task_prompt function."""

    def test_includes_goal(self, sample_seed: Seed) -> None:
        """Test that task prompt includes the goal."""
        prompt = build_task_prompt(sample_seed)
        assert sample_seed.goal in prompt

    def test_includes_acceptance_criteria(self, sample_seed: Seed) -> None:
        """Test that task prompt includes all acceptance criteria."""
        prompt = build_task_prompt(sample_seed)
        assert "Tasks can be created" in prompt
        assert "Tasks can be listed" in prompt
        assert "Tasks can be deleted" in prompt

    def test_numbers_acceptance_criteria(self, sample_seed: Seed) -> None:
        """Test that acceptance criteria are numbered."""
        prompt = build_task_prompt(sample_seed)
        assert "1." in prompt
        assert "2." in prompt
        assert "3." in prompt

    def test_does_not_duplicate_system_ontology_lens(self, sample_seed: Seed) -> None:
        """Task prompt remains focused on concrete acceptance criteria."""
        prompt = build_task_prompt(sample_seed)

        assert "## Ontology / Conceptual Lens" not in prompt
        assert "conceptual lens for execution decisions" not in prompt

    def test_includes_auto_recursion_guard(self, sample_seed: Seed) -> None:
        prompt = build_task_prompt(sample_seed)

        assert "Auto Recursion Guard" in prompt
        assert "ouroboros_auto" in prompt
        assert "nested auto session" in prompt


class TestOrchestratorResult:
    """Tests for OrchestratorResult dataclass."""

    def test_create_successful_result(self) -> None:
        """Test creating a successful result."""
        result = OrchestratorResult(
            success=True,
            session_id="sess_123",
            execution_id="exec_456",
            summary={"tasks_completed": 3},
            messages_processed=50,
            final_message="All tasks completed",
            duration_seconds=120.5,
        )

        assert result.success is True
        assert result.session_id == "sess_123"
        assert result.execution_id == "exec_456"
        assert result.summary["tasks_completed"] == 3
        assert result.messages_processed == 50
        assert result.duration_seconds == 120.5

    def test_result_is_frozen(self) -> None:
        """Test that OrchestratorResult is immutable."""
        result = OrchestratorResult(
            success=True,
            session_id="s",
            execution_id="e",
        )
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore


class TestACExecutionResult:
    """Tests for AC execution result convenience properties."""

    def test_decomposition_trustworthy_reflects_finalized_decision(self) -> None:
        """ACExecutionResult exposes trusted split status without callers parsing records."""
        from ouroboros.orchestrator.decomposition_policy import (
            DecompositionChild,
            DecompositionDecisionRecord,
            DecompositionDisposition,
            DecompositionSource,
            SemanticAttestationStatus,
            StructuralCheckStatus,
        )

        decision = DecompositionDecisionRecord(
            node_id="node_abc",
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.SPLIT,
            children=(
                DecompositionChild(
                    description="Implement config field",
                    coverage_claims=("config",),
                    verification_hint="unit",
                ),
                DecompositionChild(
                    description="Wire runner field",
                    coverage_claims=("runner",),
                    verification_hint="unit",
                ),
            ),
            structural_status=StructuralCheckStatus.PASSED,
            semantic_status=SemanticAttestationStatus.ESTABLISHED,
            trustworthy=True,
        )

        result = ACExecutionResult(
            ac_index=0,
            ac_content="Split a trusted task",
            success=True,
            decomposition_decision=decision,
        )

        assert result.decomposition_trustworthy is True
        assert (
            ACExecutionResult(
                ac_index=1, ac_content="Atomic task", success=True
            ).decomposition_trustworthy
            is False
        )


class TestExecutionEventEmitter:
    """Tests for decomposition event helpers used by executor wiring."""

    @pytest.mark.asyncio
    async def test_emit_decomposition_decision_finalized_persists_bounded_payload(
        self,
    ) -> None:
        from ouroboros.orchestrator.decomposition_policy import (
            BounceCause,
            DecompositionDecisionRecord,
            DecompositionDisposition,
            DecompositionSource,
        )
        from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
        from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity

        safe_emit = AsyncMock(return_value=True)
        event_store = AsyncMock()
        emitter = ExecutionEventEmitter(event_store, safe_emit_event=safe_emit)
        node = ExecutionNodeIdentity.root(execution_context_id="exec_1", ac_index=0)
        decision = DecompositionDecisionRecord(
            node_id=node.node_id,
            source=DecompositionSource.BOUNCE,
            disposition=DecompositionDisposition.ESCALATED,
            cause=BounceCause.TOO_BIG,
            reasons=("decomposition_depth_cap",),
            compromise_reason="depth_cap_forced_atomic",
        )

        await emitter.emit_decomposition_decision_finalized(
            execution_id="exec_1",
            session_id="sess_1",
            mode="bounce_only",
            node_identity=node,
            decision=decision,
        )

        event = event_store.append.await_args.args[0]
        safe_emit.assert_not_awaited()
        assert event.type == "execution.decomposition.decision_finalized"
        assert event.aggregate_id == "exec_1"
        assert event.data["node_id"] == node.node_id
        assert event.data["mode"] == "bounce_only"
        assert event.data["disposition"] == "ESCALATED"
        assert event.data["child_count"] == 0

    @pytest.mark.asyncio
    async def test_emit_bounce_classified_persists_classification_payload(self) -> None:
        from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
        from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity

        safe_emit = AsyncMock(return_value=True)
        event_store = AsyncMock()
        emitter = ExecutionEventEmitter(event_store, safe_emit_event=safe_emit)
        node = ExecutionNodeIdentity.root(execution_context_id="exec_1", ac_index=0)

        await emitter.emit_bounce_classified(
            execution_id="exec_1",
            session_id="sess_1",
            node_identity=node,
            cause="TOO_BIG",
            rationale="Attempt evidence shows distinct parent scope remains.",
            failure_class="SCOPE_CREEP",
            retry_admission="REDISPATCH",
            evidence_refs=(),
            trace_summary="attempted_tool_count=1\nremaining_artifact_count=1",
        )

        event = event_store.append.await_args.args[0]
        safe_emit.assert_not_awaited()
        assert event.type == "execution.decomposition.bounce_classified"
        assert event.aggregate_id == "exec_1"
        assert event.data["node_id"] == node.node_id
        assert event.data["cause"] == "TOO_BIG"
        assert event.data["failure_class"] == "SCOPE_CREEP"
        assert event.data["retry_admission"] == "REDISPATCH"
        assert event.data["evidence_refs"] == []
        assert event.data["trace_summary"] == ("attempted_tool_count=1\nremaining_artifact_count=1")


class TestOrchestratorRunner:
    """Tests for OrchestratorRunner."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create a mock Claude agent adapter."""
        Path("/tmp/project").mkdir(parents=True, exist_ok=True)
        adapter = MagicMock()
        adapter.runtime_backend = "opencode"
        adapter.llm_backend = "test-llm"
        adapter._model = "test-model"
        adapter.working_directory = "/tmp/project"
        adapter.permission_mode = "acceptEdits"
        return adapter

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        store.query_execution_related_events = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock Rich console."""
        return MagicMock()

    @pytest.fixture
    def runner(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> OrchestratorRunner:
        """Create a runner with mocked dependencies."""
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _allow_mocked_precreated_durable_state(runner)
        return runner

    async def test_close_adapter_awaits_runtime_lifecycle(self, runner) -> None:
        runner._adapter.aclose = AsyncMock()

        await runner._close_adapter()

        runner._adapter.aclose.assert_awaited_once()

    def test_param_degradation_notice_surfaces_for_serial_runner(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        mock_adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
        )
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        runner._announce_param_degradations(system_prompt="be terse", tools=None)
        runner._announce_param_degradations(system_prompt="be terse", tools=None)

        assert mock_console.print.call_count == 1
        notice = mock_console.print.call_args.args[0]
        assert "system_prompt" in notice
        assert "opencode" in notice

    def test_decomposition_mode_override_and_disable_are_resolved_on_runner(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Runner resolves explicit decomposition mode before executor dispatch."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            decomposition_mode="bounce_only",
        )
        disabled_runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            enable_decomposition=False,
            decomposition_mode="off",
        )
        with pytest.raises(ValueError, match="Unsupported decomposition_mode"):
            OrchestratorRunner(
                mock_adapter,
                mock_event_store,
                mock_console,
                decomposition_mode="preflight",  # type: ignore[arg-type]
            )

        assert runner._decomposition_mode == "bounce_only"
        assert disabled_runner._decomposition_mode == "off"

    def test_runner_preserves_legacy_depth_above_durable_replay_boundary(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            max_decomposition_depth=MAX_DECOMPOSITION_DEPTH + 1,
        )

        assert runner._max_decomposition_depth == 5
        runner._model_router = MagicMock()
        runner._route_economics = MagicMock()
        mock_adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            model_override_support=ParamSupport.NATIVE,
        )
        assert runner._bounded_route_runtime_active() is False
        mock_adapter.execute_task.assert_not_called()

    def test_runner_accepts_shared_persistable_maximum_depth(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            max_decomposition_depth=MAX_DECOMPOSITION_DEPTH,
        )

        assert runner._max_decomposition_depth == MAX_DECOMPOSITION_DEPTH == 4

    @pytest.mark.asyncio
    async def test_route_call_effort_emits_observability_only_event(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """The runner's direct-path effort event is observability, not proof input.

        It must carry NO ``ac_id`` (a direct run is a single top-level unit with no
        per-AC decomposition) and be marked ``is_decomposed_child=False`` with
        ``call_site="runner"`` — so the deterministic frugality proof excludes it by
        design (both ``assemble_triads`` ac_id-skip and ``counts_in_proof`` only
        admitting decomposed children) rather than mis-joining a whole-seed run.
        """
        mock_adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            reasoning_effort_support=ParamSupport.NATIVE,
        )
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        runner._reasoning_effort = "high"  # NATIVE runtime enforces it

        kwargs = await runner._route_call_effort(execution_id="exec_1", session_id="sess_1")

        # Enforcing runtime is handed the level for execute_task.
        assert kwargs == {"reasoning_effort": "high"}
        # Exactly one effort_routed event was emitted, with the proof-excluded shape.
        appended = [c.args[0] for c in mock_event_store.append.call_args_list]
        routed = [e for e in appended if e.type == "execution.ac.effort_routed"]
        assert len(routed) == 1
        data = routed[0].data
        assert "ac_id" not in data  # no per-AC identity → proof skips it
        assert data["is_decomposed_child"] is False
        assert data["call_site"] == "runner"
        assert data["effort_level"] == "high"
        assert data["effort_mode"] == "enforced"
        assessed = [e for e in appended if e.type == "execution.ac.investment_assessed"]
        assert len(assessed) == 1
        assert "ac_id" not in assessed[0].data
        assert assessed[0].data["provenance"] == "absent"
        assert assessed[0].data["can_cheapen"] is False
        assert assessed[0].data["call_site"] == "runner"

    @pytest.mark.asyncio
    async def test_route_call_effort_persist_failure_does_not_abort(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """A degraded event store must not abort dispatch/resume.

        _route_call_effort runs before execute_task on the direct and resume paths,
        and the effort event is observability-only — so a failing append degrades to
        a warning and still returns the execute_task kwargs, never propagating.
        """
        mock_adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            reasoning_effort_support=ParamSupport.NATIVE,
        )
        mock_event_store.append.side_effect = RuntimeError("event store unavailable")
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        runner._reasoning_effort = "high"

        # Must not raise even though append fails; kwargs still come back for dispatch.
        kwargs = await runner._route_call_effort(execution_id="exec_1", session_id="sess_1")

        assert kwargs == {"reasoning_effort": "high"}
        assert mock_event_store.append.await_count >= 1

    @pytest.mark.asyncio
    async def test_route_call_effort_advised_runtime_emits_no_kwarg(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """An advised (non-enforcing) runtime records the event but gets no kwarg."""
        mock_adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            # reasoning_effort_support defaults to IGNORED → advised
        )
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        runner._reasoning_effort = "high"

        kwargs = await runner._route_call_effort(execution_id="exec_1", session_id="sess_1")

        assert kwargs == {}  # never hand the kwarg to a runtime that ignores it
        appended = [c.args[0] for c in mock_event_store.append.call_args_list]
        routed = [e for e in appended if e.type == "execution.ac.effort_routed"]
        assert len(routed) == 1
        assert routed[0].data["effort_mode"] == "advised"

    @pytest.mark.asyncio
    async def test_execute_seed_success(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Test successful seed execution."""

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(type="tool", content="Reading", tool_name="Read")
            yield AgentMessage(
                type="result",
                content="Task completed successfully",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        # Mock session creation using Result type
        from ouroboros.core.types import Result

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_completed", mock_mark_completed):
                result = await runner.execute_seed(sample_seed)

        assert result.is_ok
        assert result.value.success is True
        # Parallel executor: 3 ACs × 3 messages each = 9 total
        assert result.value.messages_processed == 9

    @pytest.mark.asyncio
    async def test_execute_seed_retries_once_with_lateral_recovery_directive(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Sequential run failures should get one same-session lateral recovery retry."""
        runtime_handle = RuntimeHandle(
            backend="opencode",
            native_session_id="oc-session-recovery",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
        )
        prompts: list[str] = []
        resume_handles: list[RuntimeHandle | None] = []

        async def mock_execute(prompt: str, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            prompts.append(prompt)
            resume_handles.append(kwargs.get("resume_handle"))
            if len(prompts) == 1:
                yield AgentMessage(
                    type="assistant",
                    content="Trying the original path",
                    resume_handle=runtime_handle,
                )
                yield AgentMessage(
                    type="result",
                    content="Tests still fail",
                    data={"subtype": "error"},
                    resume_handle=runtime_handle,
                )
                return

            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE] Recovered",
                data={"subtype": "success"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        assert result.value.success is True
        assert len(prompts) == 2
        assert "Lateral Recovery Directive" in prompts[1]
        assert "Selected persona: hacker" in prompts[1]
        assert resume_handles[1] == runtime_handle

        recovery_event = next(
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "resilience.recovery.applied"
        )
        assert recovery_event.data["pattern"] == "spinning"
        assert recovery_event.data["persona"] == "hacker"

    @pytest.mark.asyncio
    async def test_execute_seed_direct_walks_bounded_routes_in_fresh_sessions(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
        from ouroboros.orchestrator.model_routing import build_model_router

        economics = EconomicsConfig(  # type: ignore[arg-type]
            default_tier="frugal",
            escalation_threshold=2,
            tiers={
                "frugal": TierConfig(
                    cost_factor=1,
                    models=[ModelConfig(provider="anthropic", model="haiku-x")],
                ),
                "standard": TierConfig(
                    cost_factor=10,
                    models=[ModelConfig(provider="anthropic", model="sonnet-x")],
                ),
                "frontier": TierConfig(
                    cost_factor=30,
                    models=[ModelConfig(provider="anthropic", model="opus-x")],
                ),
            },
        )
        mock_adapter.runtime_backend = "claude"
        mock_adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            model_override_support=ParamSupport.NATIVE,
        )
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _allow_mocked_precreated_durable_state(runner)
        runner._route_economics = economics
        runner._model_router = build_model_router(economics, runtime_backend="claude")
        assert runner._model_router is not None
        models: list[str] = []
        resume_handles: list[RuntimeHandle | None] = []

        async def mock_execute(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            resume_handles.append(kwargs.get("resume_handle"))
            failed = len(models) < 2
            yield AgentMessage(
                type="result",
                content="failed" if failed else "[TASK_COMPLETE]",
                data={"subtype": "error" if failed else "success"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id=f"route-{len(models)}",
                    cwd="/tmp/project",
                    approval_mode="acceptEdits",
                ),
            )

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok and result.value.success is True
        assert models == ["sonnet-x", "opus-x"]
        assert resume_handles[1:] == [None]
        route_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_observed"
        ]
        assert [event.data["observation"]["route_id"] for event in route_events] == [
            "compat:claude:standard",
            "compat:claude:frontier",
        ]
        assert all(event.data["final_acceptance_declared"] is False for event in route_events)

    @pytest.mark.asyncio
    async def test_direct_live_successor_cost_drift_blocks_before_second_provider_effect(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        models: list[str] = []

        async def mock_execute(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            yield AgentMessage(
                type="result",
                content="evidence missing",
                data={"subtype": "error"},
            )

        mock_adapter.execute_task = mock_execute

        async def append_and_drift(event: BaseEvent) -> None:
            if event.type != "execution.ac.route_observed":
                return
            assert runner._route_economics is not None
            tiers = dict(runner._route_economics.tiers)
            tiers["frontier"] = tiers["frontier"].model_copy(update={"cost_factor": 99})
            runner._route_economics = runner._route_economics.model_copy(update={"tiers": tiers})

        mock_event_store.append.side_effect = append_and_drift

        async def create_session(*_args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        with patch.object(runner._session_repo, "create_session", create_session):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert models == ["sonnet-x"]
        assert result.is_err
        assert "Route admission became stale" in str(result.error)

    @pytest.mark.asyncio
    async def test_direct_bounded_route_cancellation_never_dispatches_successor(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        from ouroboros.orchestrator.events import create_session_cancelled_event
        from ouroboros.orchestrator.runner import CANCELLATION_CHECK_INTERVAL

        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        models: list[str] = []

        async def mock_execute(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            for index in range(CANCELLATION_CHECK_INTERVAL + 1):
                yield AgentMessage(type="assistant", content=f"Message {index}")
            yield AgentMessage(
                type="result",
                content="failed after cancellation",
                data={"subtype": "error"},
            )

        mock_adapter.execute_task = mock_execute
        cancel_event = create_session_cancelled_event("session-1", "User requested")
        mock_event_store.query_events = AsyncMock(
            side_effect=[[], [], [cancel_event], [], [], [], []]
        )

        async def create_session(*_args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        with (
            patch.object(runner._session_repo, "create_session", create_session),
            patch.object(
                runner._session_repo,
                "mark_cancelled",
                AsyncMock(return_value=Result.ok(None)),
            ),
        ):
            result = await runner.execute_seed(
                sample_seed,
                parallel=False,
                execution_id="execution-1",
                session_id="session-1",
            )

        assert result.is_ok and result.value.success is False
        assert models == ["sonnet-x"]
        route_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_observed"
        ]
        assert route_events == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("assistant_messages", range(4))
    async def test_direct_short_final_turn_cancellation_population_stops_before_observation(
        self,
        assistant_messages: int,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Every final turn below the five-message poll interval uses the choke-point check."""

        from ouroboros.orchestrator.runner import CANCELLATION_CHECK_INTERVAL

        assert assistant_messages + 1 < CANCELLATION_CHECK_INTERVAL
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        session_id = f"session-short-cancel-{assistant_messages}"
        models: list[str] = []

        async def mock_execute(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            for index in range(assistant_messages):
                yield AgentMessage(type="assistant", content=f"Message {index}")
            await request_cancellation(session_id, reason="short final turn", cancelled_by="test")
            yield AgentMessage(
                type="result",
                content="failed after short-turn cancellation",
                data={"subtype": "error"},
            )

        mock_adapter.execute_task = mock_execute
        mock_event_store.query_events = AsyncMock(return_value=[])

        async def create_session(*_args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        with (
            patch.object(runner._session_repo, "create_session", create_session),
            patch.object(
                runner._session_repo,
                "mark_cancelled",
                AsyncMock(return_value=Result.ok(None)),
            ),
        ):
            result = await runner.execute_seed(
                sample_seed,
                parallel=False,
                execution_id="execution-short-cancel",
                session_id=session_id,
            )

        assert result.is_ok and result.value.success is False
        assert models == ["sonnet-x"]
        route_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_observed"
        ]
        assert route_events == []
        assert not await is_cancellation_requested(session_id)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status_field", "status_value"), _LONG_WINDOW_429_ENCODINGS)
    async def test_direct_bounded_usage_limit_pause_remains_resume_eligible(
        self,
        status_field: str,
        status_value: object,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        models: list[str] = []

        async def mock_execute(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            yield AgentMessage(
                type="result",
                content="Provider request failed.",
                data={
                    "subtype": "error",
                    status_field: status_value,
                    "retry_after_seconds": 7_200,
                },
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="route-paused",
                ),
            )

        mock_adapter.execute_task = mock_execute

        async def create_session(*_args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        mark_paused = AsyncMock(return_value=Result.ok(True))
        with (
            patch.object(runner._session_repo, "create_session", create_session),
            patch.object(runner._session_repo, "mark_paused", mark_paused),
        ):
            result = await runner.execute_seed(
                sample_seed,
                parallel=False,
                execution_id="execution-1",
                session_id="session-1",
            )

        assert result.is_ok and result.value.success is False
        assert models == ["sonnet-x"]
        mark_paused.assert_awaited_once()
        route_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_observed"
        ]
        assert route_events == []
        pause_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_paused"
        ]
        assert len(pause_events) == 1

        async def query_route_state(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            return pause_events if kwargs.get("event_type") == "execution.ac.route_paused" else []

        mock_event_store.query_execution_related_events = AsyncMock(side_effect=query_route_state)
        resume_state = await runner._direct_resume_route_id(
            execution_id="execution-1",
            session_id="session-1",
        )
        assert resume_state is not None
        assert resume_state.candidate.route_id == "compat:claude:standard"
        runner._retire_process_local_authority(
            session_id="session-1",
            execution_id="execution-1",
        )

    @pytest.mark.asyncio
    async def test_direct_pause_consumes_pre_effect_durable_pause_policy(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A provider cannot change the fallback pause window after contract publication."""

        monkeypatch.setenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", "1")
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)

        async def mock_execute(*_args: Any, **_kwargs: Any) -> AsyncIterator[AgentMessage]:
            monkeypatch.setenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", "9")
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again later.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="durable-pause-policy",
                ),
            )

        mock_adapter.execute_task = mock_execute

        async def create_session(*_args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        mark_paused = AsyncMock(return_value=Result.ok(True))
        with (
            patch.object(runner._session_repo, "create_session", create_session),
            patch.object(runner._session_repo, "mark_paused", mark_paused),
        ):
            result = await runner.execute_seed(
                sample_seed,
                parallel=False,
                execution_id="execution-durable-pause",
                session_id="session-durable-pause",
            )

        assert result.is_ok and result.value.success is False
        mark_paused.assert_awaited_once()
        assert mark_paused.await_args.kwargs["pause_seconds"] == 3600

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status_field", "status_value"), _LONG_WINDOW_429_ENCODINGS)
    async def test_parallel_bounded_usage_limit_pauses_without_escalation(
        self,
        status_field: str,
        status_value: object,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        seed = sample_seed.model_copy(
            update={"acceptance_criteria": (sample_seed.acceptance_criteria[0],)}
        )
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            enable_decomposition=False,
        )
        runner._run_verify_commands = False
        _enable_direct_bounded_routes(runner, mock_adapter)
        encoding_id = f"{status_field}-{type(status_value).__name__}"
        tracker = SessionTracker.create(
            f"execution-parallel-quota-{encoding_id}",
            seed.metadata.seed_id,
            session_id=f"session-parallel-quota-{encoding_id}",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            seed,
            session_id=tracker.session_id,
        )
        dependency_graph = DependencyGraph(
            nodes=(ACNode(index=0, content=seed.acceptance_criteria[0]),),
            execution_levels=((0,),),
        )
        provider_calls = 0

        async def mock_execute(*_args: Any, **_kwargs: Any) -> AsyncIterator[AgentMessage]:
            nonlocal provider_calls
            provider_calls += 1
            yield AgentMessage(
                type="result",
                content="Provider request failed.",
                data={
                    "subtype": "error",
                    status_field: status_value,
                    "retry_after_seconds": 7_200,
                },
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="parallel-route-paused",
                ),
            )

        mock_adapter.execute_task = mock_execute
        mark_paused = AsyncMock(return_value=Result.ok(True))
        mark_failed = AsyncMock(return_value=Result.ok(None))
        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(runner._session_repo, "mark_paused", mark_paused),
            patch.object(runner._session_repo, "mark_failed", mark_failed),
            patch.object(
                runner,
                "_report_frugality_retrospective",
                AsyncMock(return_value=False),
            ),
        ):
            result = await runner._execute_parallel(
                seed=seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=[],
                tool_catalog=assemble_session_tool_catalog([]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok and result.value.success is False
        assert provider_calls == 1
        mark_paused.assert_awaited_once()
        mark_failed.assert_not_awaited()
        emitted_types = {
            call.args[0].type
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) is not None
        }
        assert "execution.ac.route_observed" not in emitted_types
        assert "execution.ac.attempt_judged" not in emitted_types
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )

    @pytest.mark.asyncio
    async def test_parallel_pause_consumes_pre_effect_durable_pause_policy(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parallel pause publication consumes the same frozen fallback policy."""

        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        monkeypatch.setenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", "1")
        seed = sample_seed.model_copy(
            update={"acceptance_criteria": (sample_seed.acceptance_criteria[0],)}
        )
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            enable_decomposition=False,
        )
        runner._run_verify_commands = False
        _enable_direct_bounded_routes(runner, mock_adapter)
        tracker = SessionTracker.create(
            "execution-parallel-durable-pause",
            seed.metadata.seed_id,
            session_id="session-parallel-durable-pause",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            seed,
            session_id=tracker.session_id,
        )
        graph = DependencyGraph(
            nodes=(ACNode(index=0, content=seed.acceptance_criteria[0]),),
            execution_levels=((0,),),
        )
        pause_message = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again later.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=seed.acceptance_criteria[0],
                    success=False,
                    messages=(pause_message,),
                    final_message=pause_message.content,
                    outcome=ACExecutionOutcome.FAILED,
                ),
            ),
            success_count=0,
            failure_count=1,
            recoverable_route_pause=True,
        )

        async def provider_boundary(**_kwargs: Any) -> ParallelExecutionResult:
            monkeypatch.setenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", "9")
            return parallel_result

        mark_paused = AsyncMock(return_value=Result.ok(True))
        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                side_effect=provider_boundary,
            ),
            patch.object(runner._session_repo, "mark_paused", mark_paused),
            patch.object(
                runner,
                "_report_frugality_retrospective",
                AsyncMock(return_value=False),
            ),
        ):
            result = await runner._execute_parallel(
                seed=seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=[],
                tool_catalog=assemble_session_tool_catalog([]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok and result.value.success is False
        mark_paused.assert_awaited_once()
        assert mark_paused.await_args.kwargs["pause_seconds"] == 3600
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )

    @pytest.mark.asyncio
    async def test_parallel_route_cancellation_is_terminalized_by_runner_owner(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """The executor signal enters cancellation lifecycle, never failure routing."""

        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
        from ouroboros.orchestrator.parallel_executor import ParallelExecutionCancelled

        seed = sample_seed.model_copy(
            update={"acceptance_criteria": (sample_seed.acceptance_criteria[0],)}
        )
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            enable_decomposition=False,
        )
        _enable_direct_bounded_routes(runner, mock_adapter)
        tracker = SessionTracker.create(
            "execution-parallel-cancel",
            seed.metadata.seed_id,
            session_id="session-parallel-cancel",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            seed,
            session_id=tracker.session_id,
        )
        graph = DependencyGraph(
            nodes=(ACNode(index=0, content=seed.acceptance_criteria[0]),),
            execution_levels=((0,),),
        )
        cancellation_result: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=False,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                summary={"cancelled": True},
                messages_processed=7,
                final_message="cancelled",
                duration_seconds=0.0,
            )
        )
        handle_cancellation = AsyncMock(return_value=cancellation_result)

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo,
                "track_progress",
                AsyncMock(return_value=Result.ok(True)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                AsyncMock(
                    side_effect=ParallelExecutionCancelled(
                        tracker.session_id,
                        messages_processed=7,
                    )
                ),
            ),
            patch.object(runner, "_handle_cancellation", handle_cancellation),
        ):
            result = await runner._execute_parallel(
                seed=seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=[],
                tool_catalog=assemble_session_tool_catalog([]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result is cancellation_result
        handle_cancellation.assert_awaited_once()
        assert handle_cancellation.await_args.kwargs["messages_processed"] == 7

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_before_pause", [False, True])
    async def test_parallel_bounded_pause_resume_uses_original_owner(
        self,
        failure_before_pause: bool,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        seed = sample_seed.model_copy(
            update={"acceptance_criteria": (sample_seed.acceptance_criteria[0],)}
        )
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            enable_decomposition=False,
        )
        runner._run_verify_commands = False
        _enable_direct_bounded_routes(runner, mock_adapter)
        case_suffix = "successor" if failure_before_pause else "initial"
        tracker = SessionTracker.create(
            f"execution-parallel-resume-{case_suffix}",
            seed.metadata.seed_id,
            session_id=f"session-parallel-resume-{case_suffix}",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            seed,
            session_id=tracker.session_id,
        )
        graph = DependencyGraph(
            nodes=(ACNode(index=0, content=seed.acceptance_criteria[0]),),
            execution_levels=((0,),),
        )
        models: list[str] = []

        async def query_events(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            event_type = kwargs.get("event_type")
            return [
                call.args[0]
                for call in mock_event_store.append.await_args_list
                if getattr(call.args[0], "type", None) == event_type
            ]

        mock_event_store.query_execution_related_events = AsyncMock(side_effect=query_events)

        async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            if failure_before_pause and len(models) == 1:
                yield AgentMessage(
                    type="result",
                    content="evidence missing",
                    data={"subtype": "error"},
                )
                return
            pause_call_number = 2 if failure_before_pause else 1
            if len(models) == pause_call_number:
                yield AgentMessage(
                    type="result",
                    content="Usage limit reached. Please try again in 5 hours.",
                    data={"subtype": "error", "error_type": "CodexCliError"},
                    resume_handle=RuntimeHandle(
                        backend="claude",
                        native_session_id="parallel-owner-handle",
                    ),
                )
                return
            yield AgentMessage(
                type="result",
                content="resumed through parallel owner",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = execute_task
        mark_paused = AsyncMock(return_value=Result.ok(True))
        mark_completed = AsyncMock(return_value=Result.ok(None))
        analyze = AsyncMock(return_value=Result.ok(graph))
        tool_catalog = assemble_session_tool_catalog([])
        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                analyze,
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(runner._session_repo, "mark_paused", mark_paused),
            patch.object(runner._session_repo, "mark_completed", mark_completed),
            patch("ouroboros.orchestrator.runner.build_system_prompt", return_value="system"),
            patch.object(
                runner,
                "_get_merged_tools",
                AsyncMock(return_value=([], None, tool_catalog)),
            ),
            patch.object(
                runner,
                "_bind_execution_tool_authority",
                return_value=(tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY], False),
            ),
            patch.object(
                runner,
                "_report_frugality_retrospective",
                AsyncMock(return_value=False),
            ),
        ):
            first = await runner._execute_parallel(
                seed=seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=[],
                tool_catalog=tool_catalog,
                system_prompt="system",
                start_time=tracker.start_time,
                execution_contract=tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
            )
            assert first.is_ok and first.value.success is False

            paused_tracker = tracker.with_status(SessionStatus.PAUSED).with_progress(
                {
                    "routing_resume_owner": "parallel",
                    "routing_parallel_force_sequential": False,
                    "routing_parallel_plan": runner._serialize_parallel_resume_plan(
                        graph.to_execution_plan()
                    ),
                    "routing_parallel_externally_satisfied_acs": {},
                }
            )
            direct_resume = AsyncMock(side_effect=AssertionError("direct owner entered"))
            parallel_resume = AsyncMock(
                return_value=Result.ok(
                    OrchestratorResult(
                        success=True,
                        session_id=paused_tracker.session_id,
                        execution_id=paused_tracker.execution_id,
                        summary={},
                        messages_processed=0,
                        final_message="parallel owner resumed",
                        duration_seconds=0.0,
                    )
                )
            )
            with (
                patch.object(
                    runner._session_repo,
                    "reconstruct_session",
                    AsyncMock(return_value=Result.ok(paused_tracker)),
                ),
                patch.object(runner, "_direct_resume_route_id", direct_resume),
                patch.object(runner, "_execute_parallel", parallel_resume),
            ):
                resumed = await runner.resume_session(paused_tracker.session_id, seed)

        assert resumed.is_ok and resumed.value.success is True
        assert models == (["sonnet-x", "opus-x"] if failure_before_pause else ["sonnet-x"])
        direct_resume.assert_not_awaited()
        parallel_resume.assert_awaited_once()
        restored_plan = parallel_resume.await_args.kwargs["resume_execution_plan"]
        assert tuple(node.index for node in restored_plan.nodes) == (0,)
        assert tuple(stage.ac_indices for stage in restored_plan.stages) == ((0,),)
        assert analyze.await_count == 1
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )

    @pytest.mark.asyncio
    async def test_parallel_resume_restores_externally_satisfied_partial_stage_state(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        seed = sample_seed.model_copy(
            update={
                "acceptance_criteria": (
                    sample_seed.acceptance_criteria[0],
                    AcceptanceCriterionSpec(description="paused sibling"),
                )
            }
        )
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            enable_decomposition=False,
        )
        mock_adapter.aclose = AsyncMock()
        tracker = SessionTracker.create(
            "execution-external-resume",
            seed.metadata.seed_id,
            session_id="session-external-resume",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            seed,
            session_id=tracker.session_id,
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=ac_texts(seed)[0]),
                ACNode(index=1, content=ac_texts(seed)[1]),
            ),
            execution_levels=((0, 1),),
        )
        external = {0: {"reason": "already verified", "commit": "abc123"}}
        paused_tracker = tracker.with_status(SessionStatus.PAUSED).with_progress(
            {
                "routing_resume_owner": "parallel",
                "routing_parallel_force_sequential": False,
                "routing_parallel_plan": runner._serialize_parallel_resume_plan(
                    graph.to_execution_plan()
                ),
                "routing_parallel_externally_satisfied_acs": (
                    runner._serialize_parallel_external_satisfaction(seed, external)
                ),
            }
        )
        parallel_resume = AsyncMock(
            return_value=Result.ok(
                OrchestratorResult(
                    success=True,
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    summary={},
                    messages_processed=0,
                    final_message="resumed",
                    duration_seconds=0.0,
                )
            )
        )

        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch.object(runner, "_execute_parallel", parallel_resume),
        ):
            result = await runner.resume_session(paused_tracker.session_id, seed)

        assert result.is_ok
        assert parallel_resume.await_args.kwargs["externally_satisfied_acs"] == external
        mock_adapter.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_direct_bounded_paused_route_actually_resumes_same_provider_handle(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        runtime_handle = RuntimeHandle(
            backend="claude",
            native_session_id="route-paused",
        )
        paused_tracker = SessionTracker.create(
            "execution-route-resume",
            sample_seed.metadata.seed_id,
            session_id="session-route-resume",
        ).with_status(SessionStatus.PAUSED)
        paused_tracker = paused_tracker.with_progress({"runtime": runtime_handle.to_dict()})
        paused_tracker = _attach_live_process_local_contract(
            runner,
            paused_tracker,
            sample_seed,
            session_id="session-route-resume",
        )
        captured_handles: list[RuntimeHandle | None] = []
        captured_models: list[str] = []

        async def mock_execute(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            captured_handles.append(kwargs.get("resume_handle"))
            captured_models.append(kwargs["model"])
            yield AgentMessage(
                type="result",
                content="Resumed successfully",
                data={"subtype": "success"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute
        from ouroboros.orchestrator.route_compat import build_route_compat_projection

        projection = build_route_compat_projection(
            runner._route_economics,
            model_router=runner._model_router,
            runtime_backend="claude",
        )
        assert projection is not None
        paused_candidate = next(
            candidate
            for candidate in projection.registry.candidates
            if candidate.route_id == "compat:claude:standard"
        )
        pause_event = BaseEvent(
            type="execution.ac.route_paused",
            aggregate_type="execution",
            aggregate_id="execution-route-resume",
            data={
                "schema_version": 1,
                "execution_id": "execution-route-resume",
                "session_id": "session-route-resume",
                "root_ac_index": None,
                "call_site": "runner",
                "episode_id": (
                    "route:" + hashlib.sha256(b"execution-route-resume\0direct").hexdigest()
                ),
                "attempt_index": 0,
                "prior_route_ids": [],
                "route": paused_candidate.to_contract_data(),
                "recoverable_pause": True,
                "final_acceptance_declared": False,
            },
        )

        async def query_route_state(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            return [pause_event] if kwargs.get("event_type") == "execution.ac.route_paused" else []

        mock_event_store.query_execution_related_events = AsyncMock(side_effect=query_route_state)
        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
        ):
            result = await runner.resume_session("session-route-resume", sample_seed)

        assert result.is_ok and result.value.success is True
        assert len(captured_handles) == 1
        assert captured_handles[0] is not None
        assert captured_handles[0].native_session_id == "route-paused"
        assert captured_models == ["sonnet-x"]
        resumed_observations = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_observed"
        ]
        assert len(resumed_observations) == 1
        assert (
            resumed_observations[0].data["observation"]["verifier_outcome"] == "attempt_succeeded"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("scenario", "expected_calls"),
        [
            ("initial_handleless_pause", 1),
            ("successor_handleless_pause", 2),
            ("pause_handle_persistence_failure", 1),
            ("hard_block", 1),
        ],
    )
    async def test_live_direct_nonreplayable_boundaries_finalize_blocked_without_pause(
        self,
        scenario: str,
        expected_calls: int,
        mock_adapter: MagicMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Any,
    ) -> None:
        """Every non-replayable direct boundary converges to one durable handoff."""

        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / f'{scenario}.db'}")
        await store.initialize()
        seed = sample_seed.model_copy(
            update={"acceptance_criteria": (sample_seed.acceptance_criteria[0],)}
        )
        runner = OrchestratorRunner(
            mock_adapter,
            store,
            mock_console,
            enable_decomposition=False,
        )
        runner._run_verify_commands = False
        _enable_direct_bounded_routes(runner, mock_adapter)
        mock_adapter.llm_backend = "test-llm"
        mock_adapter._model = "test-model"
        provider_calls: list[dict[str, Any]] = []

        async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            provider_calls.append(dict(kwargs))
            if scenario == "successor_handleless_pause" and len(provider_calls) == 1:
                yield AgentMessage(
                    type="result",
                    content="evidence missing",
                    data={"subtype": "error"},
                )
                return
            if scenario == "hard_block":
                yield AgentMessage(
                    type="result",
                    content="Permission denied by the host runtime.",
                    data={"subtype": "error", "error_type": "PermissionError"},
                )
                return
            if scenario == "pause_handle_persistence_failure":
                yield AgentMessage(
                    type="result",
                    content="Usage limit reached. Please try again in 5 hours.",
                    data={"subtype": "error", "error_type": "CodexCliError"},
                    resume_handle=RuntimeHandle(
                        backend="claude",
                        native_session_id="unpersisted-direct-pause",
                        cwd=str(tmp_path),
                    ),
                )
                return
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
            )

        mock_adapter.execute_task = execute_task
        execution_id = f"execution-{scenario}"
        session_id = f"session-{scenario}"
        original_track_progress = runner._session_repo.track_progress

        async def track_progress(
            tracked_session_id: str,
            progress: dict[str, Any],
        ) -> Any:
            runtime = progress.get("runtime")
            if (
                scenario == "pause_handle_persistence_failure"
                and isinstance(runtime, dict)
                and runtime.get("native_session_id") == "unpersisted-direct-pause"
            ):
                return Result.err(PersistenceError("runtime progress unavailable"))
            return await original_track_progress(tracked_session_id, progress)

        try:
            with (
                patch.object(
                    runner._session_repo,
                    "track_progress",
                    side_effect=track_progress,
                ),
                patch.object(
                    runner,
                    "_report_frugality_retrospective",
                    AsyncMock(return_value=False),
                ),
            ):
                result = await runner.execute_seed(
                    seed,
                    parallel=False,
                    execution_id=execution_id,
                    session_id=session_id,
                )

            assert result.is_ok and result.value.success is False
            assert len(provider_calls) == expected_calls
            pause_events = await store.query_execution_related_events(
                execution_id,
                event_type="execution.ac.route_paused",
                limit=4,
            )
            assert pause_events == []
            session = await runner._session_repo.reconstruct_session(session_id)
            assert session.is_ok and session.value.status is SessionStatus.FAILED

            finalizations = await store.query_execution_related_events(
                execution_id,
                event_type="execution.ac.acceptance_finalized",
                limit=3,
            )
            assert len(finalizations) == 1
            assert finalizations[0].data["outcome"] == "blocked"
            assert finalizations[0].data["disposition"] == "blocked"
            assert finalizations[0].data["accepted"] is False

            resumed = await runner.resume_session(session_id, seed)
            assert resumed.is_err
            assert len(provider_calls) == expected_calls
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_live_direct_pause_transfers_runtime_handle_without_termination(
        self,
        mock_adapter: MagicMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Any,
    ) -> None:
        """A durable PAUSED owner keeps the exact live provider boundary intact."""
        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'direct-live-pause.db'}")
        await store.initialize()
        seed = sample_seed.model_copy(
            update={"acceptance_criteria": (sample_seed.acceptance_criteria[0],)}
        )
        runner = OrchestratorRunner(
            mock_adapter,
            store,
            mock_console,
            enable_decomposition=False,
        )
        runner._run_verify_commands = False
        _enable_direct_bounded_routes(runner, mock_adapter)
        mock_adapter.llm_backend = "test-llm"
        mock_adapter._model = "test-model"
        terminate_calls = 0

        async def terminate(_handle: RuntimeHandle) -> bool:
            nonlocal terminate_calls
            terminate_calls += 1
            return True

        live_handle = RuntimeHandle(
            backend="claude",
            native_session_id="durable-live-pause",
            cwd=str(tmp_path),
        ).bind_controls(terminate_callback=terminate)

        async def execute_task(*_args: Any, **_kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=live_handle,
            )

        mock_adapter.execute_task = execute_task
        execution_id = "execution-direct-live-pause"
        session_id = "session-direct-live-pause"
        try:
            result = await runner.execute_seed(
                seed,
                parallel=False,
                execution_id=execution_id,
                session_id=session_id,
            )

            assert result.is_ok and result.value.success is False
            assert terminate_calls == 0
            session = await runner._session_repo.reconstruct_session(session_id)
            assert session.is_ok and session.value.status is SessionStatus.PAUSED
            pauses = await store.query_execution_related_events(
                execution_id,
                event_type="execution.ac.route_paused",
                limit=2,
            )
            assert len(pauses) == 1
        finally:
            runner._retire_process_local_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            runner._unregister_session(execution_id, session_id)
            await store.close()

    @pytest.mark.asyncio
    async def test_direct_resumed_short_turn_cancellation_never_dispatches_successor(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """The resumed-route stream shares the same post-stream cancellation gate."""

        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        runtime_handle = RuntimeHandle(
            backend="claude",
            native_session_id="route-resumed-cancel",
        )
        paused_tracker = SessionTracker.create(
            "execution-route-resume-cancel",
            sample_seed.metadata.seed_id,
            session_id="session-route-resume-cancel",
        ).with_status(SessionStatus.PAUSED)
        paused_tracker = paused_tracker.with_progress({"runtime": runtime_handle.to_dict()})
        paused_tracker = _attach_live_process_local_contract(
            runner,
            paused_tracker,
            sample_seed,
            session_id=paused_tracker.session_id,
        )
        models: list[str] = []

        async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            await request_cancellation(
                paused_tracker.session_id,
                reason="cancel resumed short turn",
                cancelled_by="test",
            )
            yield AgentMessage(
                type="result",
                content="resumed route failed after cancellation",
                data={"subtype": "error"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = execute_task
        from ouroboros.orchestrator.route_compat import build_route_compat_projection

        projection = build_route_compat_projection(
            runner._route_economics,
            model_router=runner._model_router,
            runtime_backend="claude",
        )
        assert projection is not None
        paused_candidate = next(
            candidate
            for candidate in projection.registry.candidates
            if candidate.route_id == "compat:claude:standard"
        )
        pause_event = BaseEvent(
            type="execution.ac.route_paused",
            aggregate_type="execution",
            aggregate_id=paused_tracker.execution_id,
            data={
                "schema_version": 1,
                "execution_id": paused_tracker.execution_id,
                "session_id": paused_tracker.session_id,
                "root_ac_index": None,
                "call_site": "runner",
                "episode_id": (
                    "route:"
                    + hashlib.sha256(f"{paused_tracker.execution_id}\0direct".encode()).hexdigest()
                ),
                "attempt_index": 0,
                "prior_route_ids": [],
                "route": paused_candidate.to_contract_data(),
                "recoverable_pause": True,
                "final_acceptance_declared": False,
            },
        )

        async def query_route_state(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            return [pause_event] if kwargs.get("event_type") == "execution.ac.route_paused" else []

        mock_event_store.query_execution_related_events = AsyncMock(side_effect=query_route_state)
        mock_event_store.query_events = AsyncMock(return_value=[])
        try:
            with (
                patch.object(
                    runner._session_repo,
                    "reconstruct_session",
                    AsyncMock(return_value=Result.ok(paused_tracker)),
                ),
                patch.object(
                    runner._session_repo,
                    "mark_cancelled",
                    AsyncMock(return_value=Result.ok(None)),
                ),
            ):
                result = await runner.resume_session(paused_tracker.session_id, sample_seed)
        finally:
            await clear_cancellation(paused_tracker.session_id)

        assert result.is_ok and result.value.success is False
        assert models == ["sonnet-x"]
        observations = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_observed"
        ]
        assert observations == []

    @pytest.mark.asyncio
    async def test_direct_bounded_resumed_failure_escalates_then_seals_success(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        runtime_handle = RuntimeHandle(
            backend="claude",
            native_session_id="route-resumed-standard",
        )
        paused_tracker = SessionTracker.create(
            "execution-route-resume-failure",
            sample_seed.metadata.seed_id,
            session_id="session-route-resume-failure",
        ).with_status(SessionStatus.PAUSED)
        paused_tracker = paused_tracker.with_progress({"runtime": runtime_handle.to_dict()})
        paused_tracker = _attach_live_process_local_contract(
            runner,
            paused_tracker,
            sample_seed,
            session_id=paused_tracker.session_id,
        )
        models: list[str] = []
        handles: list[RuntimeHandle | None] = []
        prompts: list[str] = []

        execution_contract = paused_tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
        persisted_strategy = runner._execution_strategy_snapshot(
            execution_contract,
            require_bound=False,
        )
        persisted_suffix = persisted_strategy.get_task_prompt_suffix()

        from ouroboros.orchestrator import execution_strategy
        from ouroboros.orchestrator.execution_strategy import CodeStrategy

        class DriftedCodeStrategy(CodeStrategy):
            def get_task_prompt_suffix(self) -> str:
                return "MUTATED LIVE REGISTRY SUFFIX"

        monkeypatch.setitem(execution_strategy._STRATEGY_REGISTRY, "code", DriftedCodeStrategy())

        async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            handles.append(kwargs.get("resume_handle"))
            prompts.append(kwargs["prompt"])
            if len(models) == 1:
                yield AgentMessage(
                    type="result",
                    content="resumed route still lacks evidence",
                    data={"subtype": "error"},
                    resume_handle=runtime_handle,
                )
                return
            yield AgentMessage(
                type="result",
                content="frontier successor succeeded",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = execute_task
        from ouroboros.orchestrator.route_compat import build_route_compat_projection

        projection = build_route_compat_projection(
            runner._route_economics,
            model_router=runner._model_router,
            runtime_backend="claude",
        )
        assert projection is not None
        paused_candidate = next(
            candidate
            for candidate in projection.registry.candidates
            if candidate.route_id == "compat:claude:standard"
        )
        episode_id = (
            "route:" + hashlib.sha256(b"execution-route-resume-failure\0direct").hexdigest()
        )
        pause_event = BaseEvent(
            type="execution.ac.route_paused",
            aggregate_type="execution",
            aggregate_id=paused_tracker.execution_id,
            data={
                "schema_version": 1,
                "execution_id": paused_tracker.execution_id,
                "session_id": paused_tracker.session_id,
                "root_ac_index": None,
                "call_site": "runner",
                "episode_id": episode_id,
                "attempt_index": 0,
                "prior_route_ids": [],
                "route": paused_candidate.to_contract_data(),
                "recoverable_pause": True,
                "final_acceptance_declared": False,
            },
        )

        async def query_route_state(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            return [pause_event] if kwargs.get("event_type") == "execution.ac.route_paused" else []

        mock_event_store.query_execution_related_events = AsyncMock(side_effect=query_route_state)
        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
        ):
            result = await runner.resume_session(paused_tracker.session_id, sample_seed)

        assert result.is_ok and result.value.success is True
        assert models == ["sonnet-x", "opus-x"]
        assert handles[0] is not None
        assert handles[0].native_session_id == runtime_handle.native_session_id
        assert handles[1] is None
        assert len(prompts) == 2
        assert all(persisted_suffix in prompt for prompt in prompts)
        assert all("MUTATED LIVE REGISTRY SUFFIX" not in prompt for prompt in prompts)
        observations = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_observed"
        ]
        assert [event.data["observation"]["verifier_outcome"] for event in observations] == [
            "failed",
            "attempt_succeeded",
        ]
        assert observations[0].data["decision"]["selected_route"]["route_id"] == (
            "compat:claude:frontier"
        )

    @pytest.mark.asyncio
    async def test_direct_pause_after_escalation_resumes_exact_successor_route(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        models: list[str] = []

        async def mock_execute(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            models.append(kwargs["model"])
            if len(models) == 1:
                yield AgentMessage(
                    type="result",
                    content="evidence missing",
                    data={"subtype": "error"},
                    resume_handle=RuntimeHandle(
                        backend="claude",
                        native_session_id="route-frugal",
                    ),
                )
                return
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="route-standard-paused",
                ),
            )

        mock_adapter.execute_task = mock_execute

        async def create_session(*_args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        with (
            patch.object(runner._session_repo, "create_session", create_session),
            patch.object(
                runner._session_repo,
                "mark_paused",
                AsyncMock(return_value=Result.ok(True)),
            ),
        ):
            result = await runner.execute_seed(
                sample_seed,
                parallel=False,
                execution_id="execution-route-escalated-pause",
                session_id="session-route-escalated-pause",
            )

        assert result.is_ok and result.value.success is False
        assert models == ["sonnet-x", "opus-x"]
        route_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_observed"
        ]
        pause_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.ac.route_paused"
        ]
        assert len(route_events) == len(pause_events) == 1
        assert pause_events[0].data["prior_route_ids"] == ["compat:claude:standard"]

        async def query_route_state(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            if kwargs.get("event_type") == "execution.ac.route_observed":
                return route_events
            if kwargs.get("event_type") == "execution.ac.route_paused":
                return pause_events
            return []

        mock_event_store.query_execution_related_events = AsyncMock(side_effect=query_route_state)
        resume_state = await runner._direct_resume_route_id(
            execution_id="execution-route-escalated-pause",
            session_id="session-route-escalated-pause",
        )
        assert resume_state is not None
        assert resume_state.candidate.route_id == "compat:claude:frontier"
        runner._retire_process_local_authority(
            session_id="session-route-escalated-pause",
            execution_id="execution-route-escalated-pause",
        )

    @pytest.mark.asyncio
    async def test_valid_65_direct_pauses_replay_without_a_total_cap(
        self,
        mock_adapter: MagicMock,
        mock_console: MagicMock,
        tmp_path: Any,
    ) -> None:
        from ouroboros.orchestrator.route_compat import (
            admit_compat_escalation_route,
            build_route_compat_projection,
        )

        execution_id = "execution-direct-pause-population"
        session_id = "session-direct-pause-population"
        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'direct-pause-population.db'}")
        await store.initialize()
        runner = OrchestratorRunner(mock_adapter, store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        projection = build_route_compat_projection(
            runner._route_economics,
            model_router=runner._model_router,
            runtime_backend="claude",
        )
        assert projection is not None
        initial = admit_compat_escalation_route(projection, effort=None)
        assert initial.selected is not None
        episode_id = "route:" + hashlib.sha256(f"{execution_id}\0direct".encode()).hexdigest()
        pauses = [
            BaseEvent(
                type="execution.ac.route_paused",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": None,
                    "call_site": "runner",
                    "episode_id": episode_id,
                    "attempt_index": 0,
                    "prior_route_ids": [],
                    "route": initial.selected.to_contract_data(),
                    "recoverable_pause": True,
                    "final_acceptance_declared": False,
                },
            )
            for _pause_number in range(65)
        ]
        try:
            await store.append_batch(pauses)
            state = await runner._direct_resume_route_id(
                execution_id=execution_id,
                session_id=session_id,
            )

            assert state is not None
            assert state.candidate == initial.selected
            assert state.attempt_index == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_direct_pause_rejects_effort_drift_from_durable_successor(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        from dataclasses import replace
        import hashlib

        from ouroboros.orchestrator.failure_taxonomy import FailureClass
        from ouroboros.orchestrator.route_compat import (
            build_compat_escalation_requirements,
            build_route_compat_projection,
        )
        from ouroboros.orchestrator.route_escalation import (
            EscalationReason,
            RouteObservation,
            VerifierOutcome,
            advance_route,
        )
        from ouroboros.orchestrator.route_policy import RouteRequirements

        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _enable_direct_bounded_routes(runner, mock_adapter)
        execution_id = "execution-direct-effort-drift"
        session_id = "session-direct-effort-drift"
        episode_id = "route:" + hashlib.sha256(f"{execution_id}\0direct".encode()).hexdigest()
        projection = build_route_compat_projection(
            runner._route_economics,
            model_router=runner._model_router,
            runtime_backend="claude",
            effort="low",
        )
        assert projection is not None
        requirements = build_compat_escalation_requirements(projection, effort="low")
        assert requirements is not None
        current = projection.registry.candidates[0]
        observation = RouteObservation.from_candidate(
            current,
            RouteRequirements(required_capabilities=requirements.required_capabilities),
            episode_id=episode_id,
            attempt_index=0,
            verifier_outcome=VerifierOutcome.FAILED,
            failure_class=FailureClass.EVIDENCE_MISSING,
            escalation_reason=EscalationReason.CLASSIFIED_FAILURE,
        )
        decision = advance_route(
            projection.registry,
            requirements,
            current_route_id=current.route_id,
            attempted_route_ids=(current.route_id,),
            failure_class=FailureClass.EVIDENCE_MISSING,
        )
        assert decision.selected is not None
        drifted_pause = replace(decision.selected, effort="high")
        observation_event = BaseEvent(
            type="execution.ac.route_observed",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "schema_version": 1,
                "execution_id": execution_id,
                "session_id": session_id,
                "root_ac_index": None,
                "call_site": "runner",
                "observation": observation.to_contract_data(),
                "decision": decision.to_contract_data(),
                "human_handoff_required": False,
                "final_acceptance_declared": False,
            },
        )
        pause_event = BaseEvent(
            type="execution.ac.route_paused",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "schema_version": 1,
                "execution_id": execution_id,
                "session_id": session_id,
                "root_ac_index": None,
                "call_site": "runner",
                "episode_id": episode_id,
                "attempt_index": 1,
                "prior_route_ids": [current.route_id],
                "route": drifted_pause.to_contract_data(),
                "recoverable_pause": True,
                "final_acceptance_declared": False,
            },
        )

        async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            if kwargs.get("event_type") == "execution.ac.route_observed":
                return [observation_event]
            if kwargs.get("event_type") == "execution.ac.route_paused":
                return [pause_event]
            return []

        mock_event_store.query_execution_related_events = AsyncMock(side_effect=query)
        with pytest.raises(OrchestratorError, match="drifted direct route pause"):
            await runner._direct_resume_route_id(
                execution_id=execution_id,
                session_id=session_id,
            )

    @pytest.mark.asyncio
    async def test_direct_resume_refuses_blocked_route_replay(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        import hashlib

        from ouroboros.orchestrator.failure_taxonomy import FailureClass
        from ouroboros.orchestrator.route_escalation import (
            EscalationAction,
            EscalationReason,
            RouteEscalationDecision,
            RouteObservation,
            VerifierOutcome,
        )
        from ouroboros.orchestrator.route_policy import RouteCandidate, RouteRequirements

        candidate = RouteCandidate(
            route_id="compat:claude:frontier",
            model="opus-x",
            harness="claude",
            effort=None,
            cost_units=30,
            persona="default",
            tool_policy="default",
            authority_identity="runtime:claude",
            ordinal=2,
        )
        observation = RouteObservation.from_candidate(
            candidate,
            RouteRequirements(),
            episode_id=("route:" + hashlib.sha256(b"execution-1\0direct").hexdigest()),
            attempt_index=2,
            verifier_outcome=VerifierOutcome.FAILED,
            failure_class=FailureClass.EVIDENCE_MISSING,
            escalation_reason=EscalationReason.ROUTES_EXHAUSTED,
        )
        decision = RouteEscalationDecision(
            action=EscalationAction.BLOCKED,
            failure_class=FailureClass.EVIDENCE_MISSING,
            selected=None,
            attempted_route_ids=(
                "compat:claude:frugal",
                "compat:claude:standard",
                "compat:claude:frontier",
            ),
            remaining_route_ids=(),
            reason=EscalationReason.ROUTES_EXHAUSTED,
        )
        mock_event_store.query_execution_related_events.return_value = [
            BaseEvent(
                type="execution.ac.route_observed",
                aggregate_type="execution",
                aggregate_id="execution-1",
                data={
                    "schema_version": 1,
                    "execution_id": "execution-1",
                    "session_id": "session-1",
                    "root_ac_index": None,
                    "call_site": "runner",
                    "observation": observation.to_contract_data(),
                    "decision": decision.to_contract_data(),
                    "human_handoff_required": True,
                    "final_acceptance_declared": False,
                },
            )
        ]

        with pytest.raises(OrchestratorError, match="human handoff"):
            await runner._direct_resume_route_id(
                execution_id="execution-1",
                session_id="session-1",
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_site", ["parallel", None])
    async def test_direct_resume_rejects_non_runner_route_evidence(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
        call_site: str | None,
    ) -> None:
        mock_event_store.query_execution_related_events.return_value = [
            BaseEvent(
                type="execution.ac.route_observed",
                aggregate_type="execution",
                aggregate_id="execution-1",
                data={
                    "execution_id": "execution-1",
                    "session_id": "session-1",
                    "root_ac_index": 0,
                    "call_site": call_site,
                },
            )
        ]

        with pytest.raises(OrchestratorError, match="another call site"):
            await runner._direct_resume_route_id(
                execution_id="execution-1",
                session_id="session-1",
            )

    @pytest.mark.asyncio
    async def test_direct_resume_rejects_unknown_observation_envelope_field(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        from ouroboros.orchestrator.failure_taxonomy import FailureClass
        from ouroboros.orchestrator.route_escalation import (
            EscalationReason,
            RouteObservation,
            VerifierOutcome,
        )
        from ouroboros.orchestrator.route_policy import RouteCandidate, RouteRequirements

        candidate = RouteCandidate(
            route_id="compat:claude:frugal",
            model="haiku-x",
            harness="claude",
            effort=None,
            cost_units=1,
            persona="default",
            tool_policy="default",
            authority_identity="runtime:claude",
        )
        observation = RouteObservation.from_candidate(
            candidate,
            RouteRequirements(),
            episode_id=("route:" + hashlib.sha256(b"execution-1\0direct").hexdigest()),
            attempt_index=0,
            verifier_outcome=VerifierOutcome.FAILED,
            failure_class=FailureClass.EVIDENCE_MISSING,
            escalation_reason=EscalationReason.CLASSIFIED_FAILURE,
        )
        mock_event_store.query_execution_related_events.return_value = [
            BaseEvent(
                type="execution.ac.route_observed",
                aggregate_type="execution",
                aggregate_id="execution-1",
                data={
                    "schema_version": 1,
                    "execution_id": "execution-1",
                    "session_id": "session-1",
                    "root_ac_index": None,
                    "call_site": "runner",
                    "observation": observation.to_contract_data(),
                    "decision": None,
                    "human_handoff_required": False,
                    "final_acceptance_declared": False,
                    "unknown_terminal_semantics": "accepted",
                },
            )
        ]

        with pytest.raises(OrchestratorError, match="invalid direct route observation envelope"):
            await runner._direct_resume_route_id(
                execution_id="execution-1",
                session_id="session-1",
            )

    @pytest.mark.asyncio
    async def test_prepare_session_forwards_seed_goal(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """prepare_session reserves a session with the seed goal persisted."""
        tracker = SessionTracker.create(
            "exec_prepared",
            sample_seed.metadata.seed_id,
            session_id="orch_prepared",
        )
        create_session = AsyncMock(return_value=Result.ok(tracker))

        with patch.object(runner._session_repo, "create_session", create_session):
            result = await runner.prepare_session(
                sample_seed,
                execution_id="exec_prepared",
                session_id="orch_prepared",
            )

        try:
            assert result.is_ok
            assert result.value.session_id == tracker.session_id
            assert result.value.progress["fat_harness_mode"] is False
            assert result.value.messages_processed == 0
            assert runner._execution_contract is not None
            create_session.assert_awaited_once_with(
                execution_id="exec_prepared",
                seed_id=sample_seed.metadata.seed_id,
                session_id="orch_prepared",
                seed_goal=sample_seed.goal,
                # Backend is forwarded so the live dashboard can provider-tag the run.
                runtime_backend=runner._adapter.runtime_backend,
                llm_backend=runner._adapter.llm_backend,
                execution_contract=runner._execution_contract,
                project_identity=resolve_project_identity(runner._adapter.working_directory),
                project_workspace=runner._effective_cwd(),
            )
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )

    @pytest.mark.asyncio
    async def test_prepare_session_publishes_matching_project_anchor_and_contract(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        repo_root = tmp_path / "repo"
        workspace = repo_root / "packages" / "app"
        workspace.mkdir(parents=True)
        mock_adapter.working_directory = str(workspace)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_cwd=str(workspace),
        )

        result = await runner.prepare_session(
            sample_seed,
            execution_id="exec-project-anchor",
            session_id="orch-project-anchor",
        )

        try:
            assert result.is_ok
            start = next(
                call.args[0]
                for call in mock_event_store.append.await_args_list
                if call.args[0].type == "orchestrator.session.started"
            )
            identity = resolve_project_identity(workspace)
            assert {
                key: start.data[key] for key in ("project_id", "project_root", "workspace_path")
            } == identity.to_event_data()
            proof = start.data["execution_contract"]["frugality_proof"]
            assert {
                "project_root": proof["project_root"],
                "workspace_path": proof["workspace_path"],
            } == identity.to_workspace_data()
        finally:
            runner._retire_process_local_authority(
                session_id="orch-project-anchor",
                execution_id="exec-project-anchor",
            )

    @pytest.mark.asyncio
    async def test_prepare_session_rejects_missing_cwd_before_publication(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        missing = tmp_path / "missing"
        mock_adapter.working_directory = str(missing)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_cwd=str(missing),
        )

        result = await runner.prepare_session(
            sample_seed,
            execution_id="exec-missing-project",
            session_id="orch-missing-project",
        )

        assert result.is_err
        assert result.error.details["invalid"] == "project_identity"
        assert not any(
            call.args[0].type == "orchestrator.session.started"
            for call in mock_event_store.append.await_args_list
        )

    @pytest.mark.asyncio
    async def test_prepare_session_rejects_task_and_managed_cwd_mismatch(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source"
        managed = tmp_path / "managed"
        selected = tmp_path / "selected"
        for directory in (source, managed, selected):
            directory.mkdir()
        workspace = TaskWorkspace(
            durable_id="cwd-mismatch",
            repo_root=str(source),
            repo_name="source",
            original_cwd=str(source),
            effective_cwd=str(managed),
            worktree_path=str(managed),
            branch="ooo/cwd-mismatch",
            lock_path=str(tmp_path / ".locks" / "cwd-mismatch.json"),
        )
        mock_adapter.working_directory = str(selected)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_cwd=str(selected),
            task_workspace=workspace,
        )

        result = await runner.prepare_session(
            sample_seed,
            execution_id="exec-cwd-mismatch",
            session_id="orch-cwd-mismatch",
        )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "runtime_cwd_mismatch"
        mock_event_store.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prepare_session_rejects_managed_relative_scope_mismatch(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source"
        worktree = tmp_path / "worktree"
        source_cwd = source / "packages" / "expected"
        execution_cwd = worktree / "packages" / "wrong"
        source_cwd.mkdir(parents=True)
        execution_cwd.mkdir(parents=True)
        workspace = TaskWorkspace(
            durable_id="scope-mismatch",
            repo_root=str(source),
            repo_name="source",
            original_cwd=str(source_cwd),
            effective_cwd=str(execution_cwd),
            worktree_path=str(worktree),
            branch="ooo/scope-mismatch",
            lock_path=str(tmp_path / ".locks" / "scope-mismatch.json"),
        )
        mock_adapter.working_directory = str(execution_cwd)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=workspace,
        )

        result = await runner.prepare_session(
            sample_seed,
            execution_id="exec-scope-mismatch",
            session_id="orch-scope-mismatch",
        )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "runtime_cwd_mismatch"
        mock_event_store.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prepare_session_rejects_stale_managed_checkout_before_publication(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source"
        source_cwd = source / "packages" / "app"
        stale_worktree = tmp_path / "managed" / "stale"
        execution_cwd = stale_worktree / "packages" / "app"
        _init_git_repo(source)
        source_cwd.mkdir(parents=True)
        execution_cwd.mkdir(parents=True)
        workspace = TaskWorkspace(
            durable_id="stale-checkout",
            repo_root=str(source),
            repo_name="source",
            original_cwd=str(source_cwd),
            effective_cwd=str(execution_cwd),
            worktree_path=str(stale_worktree),
            branch="ooo/stale-checkout",
            lock_path=str(tmp_path / ".locks" / "stale-checkout.json"),
        )
        mock_adapter.working_directory = str(execution_cwd)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=workspace,
        )

        result = await runner.prepare_session(
            sample_seed,
            execution_id="exec-stale-checkout",
            session_id="orch-stale-checkout",
        )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "project_identity_mismatch"
        mock_event_store.append.assert_not_awaited()

    @pytest.mark.parametrize(
        "source_mutation",
        ["delete", "replace", "ownership-mismatch"],
    )
    @pytest.mark.asyncio
    async def test_prepare_session_revalidates_complete_managed_identity_at_publication(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
        source_mutation: str,
    ) -> None:
        workspace = _bare_linked_task_workspace(mock_adapter, tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=workspace,
        )
        create_session = runner._session_repo.create_session

        async def mutate_source_then_create(**kwargs: Any):
            source = Path(workspace.repo_root)
            source_cwd = Path(workspace.original_cwd)
            if source_mutation == "delete":
                source_cwd.rmdir()
            elif source_mutation == "replace":
                source_cwd.rmdir()
                source_cwd.write_text("not a directory", encoding="utf-8")
            else:
                source.rename(tmp_path / "detached-source")
                _init_git_repo(source)
                (source / "packages" / "app").mkdir(parents=True)
            assert resolve_project_identity(workspace.effective_cwd) == kwargs["project_identity"]
            assert kwargs["project_task_workspace"] is workspace
            return await create_session(**kwargs)

        with patch.object(
            runner._session_repo,
            "create_session",
            new=mutate_source_then_create,
        ):
            result = await runner.prepare_session(
                sample_seed,
                execution_id=f"exec-managed-race-{source_mutation}",
                session_id=f"orch-managed-race-{source_mutation}",
            )

        assert result.is_err
        assert "Failed to create session" in result.error.message
        assert not any(
            call.args[0].type == "orchestrator.session.started"
            for call in mock_event_store.append.await_args_list
        )
        assert (
            f"orch-managed-race-{source_mutation}",
            f"exec-managed-race-{source_mutation}",
        ) not in runner._process_local_authorities

    @pytest.mark.asyncio
    async def test_prepare_session_with_unset_leader_runtime_cwd(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        runtime = LeaderDrivenWorkerRuntime(
            transport=MagicMock(cli_path="/tmp/worker"),
            runtime_backend="codex_mcp",
            llm_backend="codex",
        )
        runner = OrchestratorRunner(runtime, mock_event_store, mock_console)

        result = await runner.prepare_session(
            sample_seed,
            execution_id="exec-leader-cwd",
            session_id="orch-leader-cwd",
        )

        try:
            assert result.is_ok
            assert runtime.working_directory == str(tmp_path)
            start = next(
                call.args[0]
                for call in mock_event_store.append.await_args_list
                if call.args[0].type == "orchestrator.session.started"
            )
            assert start.data["project_root"] == str(tmp_path)
        finally:
            runner._retire_process_local_authority(
                session_id="orch-leader-cwd",
                execution_id="exec-leader-cwd",
            )

    def test_optional_runtime_cwd_is_not_inferred_from_runner_launch(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launch_cwd = tmp_path / "launch"
        later_cwd = tmp_path / "later"
        launch_cwd.mkdir()
        later_cwd.mkdir()
        mock_adapter.working_directory = None
        monkeypatch.chdir(launch_cwd)
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        monkeypatch.chdir(later_cwd)

        assert runner._effective_cwd() is None
        assert runner._project_identity() is None

    @pytest.mark.asyncio
    async def test_execute_rejects_unowned_provider_cwd_before_process_drift(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launch_cwd = tmp_path / "launch"
        later_cwd = tmp_path / "later"
        launch_cwd.mkdir()
        later_cwd.mkdir()
        with patch(
            "ouroboros.orchestrator.adapter.os.getcwd",
            side_effect=FileNotFoundError,
        ):
            runtime = CodexCliRuntime(cli_path="/usr/bin/true")
        monkeypatch.chdir(launch_cwd)
        runner = OrchestratorRunner(runtime, mock_event_store, mock_console)
        monkeypatch.chdir(later_cwd)

        assert runtime.working_directory is None
        provider_entry = MagicMock()
        with patch.object(runtime, "execute_task", provider_entry):
            result = await runner.execute_seed(
                sample_seed,
                execution_id="exec-unowned-provider-cwd",
                session_id="orch-unowned-provider-cwd",
                parallel=False,
            )

        assert result.is_err
        assert "project identity" in result.error.message.lower()
        provider_entry.assert_not_called()
        mock_event_store.append.assert_not_awaited()

    def test_relative_task_cwd_is_frozen_before_process_cwd_changes(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launch_cwd = tmp_path / "launch"
        workspace = launch_cwd / "workspace"
        later_cwd = tmp_path / "later"
        workspace.mkdir(parents=True)
        later_cwd.mkdir()
        monkeypatch.chdir(launch_cwd)
        mock_adapter.working_directory = "workspace"

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_cwd="workspace",
        )
        monkeypatch.chdir(later_cwd)

        assert runner._effective_cwd() == str(workspace)
        assert runner._project_identity() == resolve_project_identity(workspace)

    def test_relative_adapter_cwd_is_frozen_until_the_adapter_changes_it(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        launch_cwd = tmp_path / "launch"
        workspace = launch_cwd / "workspace"
        later_cwd = tmp_path / "later"
        replacement = later_cwd / "replacement"
        workspace.mkdir(parents=True)
        replacement.mkdir(parents=True)
        monkeypatch.chdir(launch_cwd)
        mock_adapter.working_directory = "workspace"
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        monkeypatch.chdir(later_cwd)
        assert runner._effective_cwd() == str(workspace)

        mock_adapter.working_directory = "replacement"
        assert runner._effective_cwd() == str(replacement)

    def test_unavailable_process_cwd_does_not_override_adapter_cwd(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_adapter.working_directory = str(tmp_path)

        with patch(
            "ouroboros.orchestrator.runner.os.getcwd",
            side_effect=FileNotFoundError,
        ):
            runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        assert runner._effective_cwd() == str(tmp_path)
        assert runner._project_identity() == resolve_project_identity(tmp_path)

    @pytest.mark.asyncio
    async def test_execute_rejects_task_cwd_when_provider_cwd_is_unavailable(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        with patch(
            "ouroboros.orchestrator.adapter.os.getcwd",
            side_effect=FileNotFoundError,
        ):
            runtime = CodexCliRuntime(cli_path="/usr/bin/true")
            runner = OrchestratorRunner(
                runtime,
                mock_event_store,
                mock_console,
                task_cwd=str(tmp_path),
            )

        provider_entry = MagicMock()
        with patch.object(runtime, "execute_task", provider_entry):
            result = await runner.execute_seed(
                sample_seed,
                execution_id="exec-cwd-owner",
                session_id="orch-cwd-owner",
                parallel=False,
            )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "runtime_cwd_mismatch"
        provider_entry.assert_not_called()
        mock_event_store.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prepare_session_rejects_resolved_project_identity_absence(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        later_identity = resolve_project_identity(tmp_path)
        identity_resolver = MagicMock(side_effect=[None, later_identity])

        with patch.object(runner, "_project_identity", identity_resolver):
            result = await runner.prepare_session(
                sample_seed,
                execution_id="exec-project-absent",
                session_id="orch-project-absent",
            )

        assert result.is_err
        assert "resolved project identity" in result.error.message
        assert identity_resolver.call_count == 1
        assert not any(
            call.args[0].type == "orchestrator.session.started"
            for call in mock_event_store.append.await_args_list
        )

    @pytest.mark.asyncio
    async def test_prepare_session_fails_when_initial_contract_cannot_persist(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """A gated session must not start if its acceptance contract is not durable."""
        tracker = SessionTracker.create(
            "exec_prepared",
            sample_seed.metadata.seed_id,
            session_id="orch_prepared",
        )
        create_session = AsyncMock(return_value=Result.ok(tracker))
        track_progress = AsyncMock(return_value=Result.err(ConfigError("store unavailable")))
        mark_failed = AsyncMock(return_value=Result.ok(None))

        with (
            patch.object(runner._session_repo, "create_session", create_session),
            patch.object(runner._session_repo, "track_progress", track_progress),
            patch.object(runner._session_repo, "mark_failed", mark_failed),
        ):
            result = await runner.prepare_session(
                sample_seed,
                execution_id="exec_prepared",
                session_id="orch_prepared",
            )

        assert result.is_err
        assert "initial session contract" in result.error.message
        assert runner._execution_contract is not None
        track_progress.assert_awaited_once_with(
            "orch_prepared",
            {
                "fat_harness_mode": False,
                "messages_processed": 0,
                "execution_contract": runner._execution_contract,
            },
        )
        mark_failed.assert_awaited_once_with(
            "orch_prepared",
            "Failed to persist initial session contract",
            {
                "session_id": "orch_prepared",
                "execution_id": "exec_prepared",
                "fat_harness_mode": False,
                "cause": "store unavailable",
            },
            acceptance_finalizations=[],
        )

    @pytest.mark.asyncio
    async def test_execute_seed_delegates_to_precreated_session(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """execute_seed should reserve IDs first, then run the precreated session."""
        tracker = SessionTracker.create(
            "exec_delegated",
            sample_seed.metadata.seed_id,
            session_id="orch_delegated",
        )
        orchestrator_result = OrchestratorResult(
            success=True,
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        prepare_session = AsyncMock(return_value=Result.ok(tracker))
        execute_precreated = AsyncMock(return_value=Result.ok(orchestrator_result))

        with (
            patch.object(runner, "prepare_session", prepare_session),
            patch.object(runner, "execute_precreated_session", execute_precreated),
        ):
            result = await runner.execute_seed(sample_seed, execution_id="exec_delegated")

        assert result.is_ok
        assert result.value == orchestrator_result
        prepare_session.assert_awaited_once_with(
            sample_seed,
            execution_id="exec_delegated",
            session_id=None,
        )
        execute_precreated.assert_awaited_once_with(
            seed=sample_seed,
            tracker=tracker,
            parallel=True,
        )

    @pytest.mark.asyncio
    async def test_execute_seed_seeds_startup_tool_catalog_on_runtime_handle(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Initial runtime startup should expose the merged tool catalog before tool calls."""
        from ouroboros.core.types import Result

        captured_kwargs: dict[str, Any] = {}
        mock_adapter._runtime_handle_backend = "opencode"
        mock_adapter._cwd = "/tmp/project"
        mock_adapter._permission_mode = "acceptEdits"

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            captured_kwargs.update(kwargs)
            resume_handle = kwargs["resume_handle"]
            assert isinstance(resume_handle, RuntimeHandle)
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success"},
                resume_handle=resume_handle,
            )

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        resume_handle = captured_kwargs["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == "opencode"
        assert resume_handle.cwd == _EXPECTED_CANONICAL_PROJECT_CWD
        assert resume_handle.metadata["tool_catalog"][0]["name"] == "Read"
        assert resume_handle.metadata["tool_catalog"][0]["id"] == "builtin:Read"
        assert resume_handle.metadata["capability_graph"][0]["name"] == "Read"
        assert resume_handle.metadata["control_plane"][0]["name"] == "Read"
        assert "Edit" in {tool["name"] for tool in resume_handle.metadata["tool_catalog"]}

    @pytest.mark.asyncio
    async def test_execute_seed_terminates_live_runtime_handle_after_completion(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Sequential runs should best-effort terminate live runtime handles on exit."""
        from ouroboros.core.types import Result

        terminate_calls = 0

        async def _terminate(_handle: RuntimeHandle) -> bool:
            nonlocal terminate_calls
            terminate_calls += 1
            return True

        live_handle = RuntimeHandle(
            backend="opencode",
            native_session_id="oc-session-live",
        ).bind_controls(terminate_callback=_terminate)

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success"},
                resume_handle=live_handle,
            )

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        assert terminate_calls == 1

    def test_build_progress_update_serializes_opencode_tool_result_metadata(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """OpenCode tool/result metadata should survive into persisted progress state."""
        from ouroboros.orchestrator.mcp_tools import (
            normalize_runtime_tool_definition,
            normalize_runtime_tool_result,
        )

        runtime_handle = RuntimeHandle(
            backend="opencode",
            native_session_id="oc-session-1",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "server_session_id": "server-42",
                "runtime_event_type": "tool.completed",
            },
        )
        message = AgentMessage(
            type="assistant",
            content="Updated src/app.py",
            data={
                "subtype": "tool_result",
                "tool_name": "Edit",
                "tool_input": {"file_path": "src/app.py"},
                "tool_definition": normalize_runtime_tool_definition(
                    "Edit",
                    {"file_path": "src/app.py"},
                ),
                "tool_result": normalize_runtime_tool_result("Updated src/app.py"),
            },
            resume_handle=runtime_handle,
        )

        progress = runner._build_progress_update(message, 3)

        assert progress["last_message_type"] == "tool_result"
        assert progress["messages_processed"] == 3
        assert progress["runtime_backend"] == "opencode"
        assert progress["runtime_event_type"] == "tool.completed"
        assert progress["tool_name"] == "Edit"
        assert progress["tool_input"] == {"file_path": "src/app.py"}
        assert progress["tool_definition"]["name"] == "Edit"
        assert progress["tool_result"]["text_content"] == "Updated src/app.py"
        assert progress["runtime"] == {
            "backend": "opencode",
            "kind": "agent_runtime",
            "native_session_id": "oc-session-1",
            "cwd": "/tmp/project",
            "approval_mode": "acceptEdits",
            "metadata": {
                "server_session_id": "server-42",
            },
        }

    def test_build_progress_update_projects_empty_tool_result_content(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Projected tool-result text should drive persisted progress previews."""
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        message = AgentMessage(
            type="assistant",
            content="",
            data={
                "subtype": "tool_result",
                "tool_name": "Edit",
                "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
            },
        )

        progress = runner._build_progress_update(message, 4)
        progress_event = runner._build_progress_event("sess_123", message, step=4)

        assert progress["last_message_type"] == "tool_result"
        assert progress["content_preview"] == "[AC_COMPLETE: 1] Done!"
        assert progress_event.data["content_preview"] == "[AC_COMPLETE: 1] Done!"
        assert progress_event.data["progress"]["last_content_preview"] == "[AC_COMPLETE: 1] Done!"

    def test_build_progress_update_keeps_message_runtime_event_type(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        message = AgentMessage(
            type="assistant",
            content="Updated src/app.py",
            data={
                "subtype": "tool_result",
                "tool_name": "Edit",
                "runtime_event_type": "tool.completed",
            },
            resume_handle=RuntimeHandle(
                backend="opencode",
                native_session_id="oc-session-1",
                metadata={"runtime_event_type": "tool.started"},
            ),
        )

        progress = runner._build_progress_update(message, 3)

        assert progress["runtime_event_type"] == "tool.completed"

    def test_build_tool_called_event_skips_named_tool_results(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        message = AgentMessage(
            type="assistant",
            content="Updated src/app.py",
            tool_name="Edit",
            data={"subtype": "tool_result"},
        )

        assert runner._build_tool_called_event("sess_123", message) is None

    def test_build_progress_update_extracts_ac_tracking_from_tool_result_payload(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Persisted progress should keep AC markers from normalized tool-result payloads."""
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        message = AgentMessage(
            type="assistant",
            content="Tool completed successfully.",
            data={
                "subtype": "tool_result",
                "tool_name": "Edit",
                "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
            },
        )

        progress = runner._build_progress_update(message, 4)
        progress_event = runner._build_progress_event("sess_123", message, step=4)

        assert progress["content_preview"] == "Tool completed successfully."
        assert progress["ac_tracking"] == {"started": [], "completed": [1]}
        assert progress_event.data["content_preview"] == "Tool completed successfully."
        assert progress_event.data["ac_tracking"] == {"started": [], "completed": [1]}
        assert progress_event.data["progress"]["ac_tracking"] == {
            "started": [],
            "completed": [1],
        }

    def test_build_progress_event_serializes_ac_tracking_metadata(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """AC marker metadata should survive into persisted progress events."""
        message = AgentMessage(
            type="assistant",
            content="[AC_START: 2] Implementing the second acceptance criterion.",
            data={"ac_tracking": {"started": [2], "completed": []}},
            resume_handle=RuntimeHandle(backend="opencode", native_session_id="oc-session-1"),
        )

        progress = runner._build_progress_update(message, 4)
        progress_event = runner._build_progress_event("sess_123", message)

        assert progress["ac_tracking"] == {"started": [2], "completed": []}
        assert progress_event.data["ac_tracking"] == {"started": [2], "completed": []}
        assert progress_event.data["progress"]["ac_tracking"] == {
            "started": [2],
            "completed": [],
        }

    @pytest.mark.asyncio
    async def test_execute_seed_emits_enriched_opencode_tool_and_progress_events(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """OpenCode-backed runs should reuse the standard tool/progress event stream."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.mcp_tools import (
            normalize_runtime_tool_definition,
            normalize_runtime_tool_result,
        )

        runtime_handle = RuntimeHandle(
            backend="opencode",
            native_session_id="oc-session-1",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "server_session_id": "server-42",
                "runtime_event_type": "session.started",
            },
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="system",
                content="OpenCode session initialized",
                resume_handle=runtime_handle,
            )
            yield AgentMessage(
                type="assistant",
                content="Calling tool: Edit: src/app.py",
                tool_name="Edit",
                data={
                    "tool_input": {"file_path": "src/app.py"},
                    "tool_definition": normalize_runtime_tool_definition(
                        "Edit",
                        {"file_path": "src/app.py"},
                    ),
                },
                resume_handle=runtime_handle,
            )
            yield AgentMessage(
                type="assistant",
                content="Updated src/app.py",
                data={
                    "subtype": "tool_result",
                    "tool_name": "Edit",
                    "tool_definition": normalize_runtime_tool_definition("Edit"),
                    "tool_result": normalize_runtime_tool_result("Updated src/app.py"),
                },
                resume_handle=runtime_handle,
            )
            yield AgentMessage(
                type="result",
                content="Task completed successfully",
                data={"subtype": "success"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok

        appended_events = [call.args[0] for call in mock_event_store.append.await_args_list]
        tool_event = next(
            event for event in appended_events if event.type == "orchestrator.tool.called"
        )
        progress_events = [
            event
            for event in appended_events
            if event.type == "orchestrator.progress.updated" and event.data.get("message_type")
        ]

        assert tool_event.data["tool_name"] == "Edit"
        assert tool_event.data["tool_input_preview"] == "file_path: src/app.py"
        assert tool_event.data["tool_input"] == {"file_path": "src/app.py"}
        assert tool_event.data["tool_definition"]["name"] == "Edit"
        assert tool_event.data["runtime_backend"] == "opencode"

        system_event = next(
            event for event in progress_events if event.data["message_type"] == "system"
        )
        tool_result_event = next(
            event for event in progress_events if event.data["message_type"] == "tool_result"
        )

        assert system_event.data["runtime_backend"] == "opencode"
        assert system_event.data["session_id"] == "oc-session-1"
        assert system_event.data["server_session_id"] == "server-42"
        assert system_event.data["resume_session_id"] == "oc-session-1"
        assert system_event.data["runtime"]["native_session_id"] == "oc-session-1"
        assert tool_result_event.data["tool_name"] == "Edit"
        assert tool_result_event.data["resume_session_id"] == "oc-session-1"
        assert tool_result_event.data["tool_result"]["text_content"] == "Updated src/app.py"

    @pytest.mark.asyncio
    async def test_execute_seed_emits_workflow_progress_with_projected_last_update(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Workflow progress updates should carry the normalized latest runtime artifact."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        runtime_handle = RuntimeHandle(
            backend="opencode",
            native_session_id="oc-session-1",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={"server_session_id": "server-42"},
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="assistant",
                content="Tool completed successfully.",
                data={
                    "subtype": "tool_result",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "src/app.py"},
                    "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
                    "runtime_event_type": "tool.completed",
                },
                resume_handle=runtime_handle,
            )
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success", "runtime_event_type": "result.completed"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok

        workflow_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "workflow.progress.updated"
        ]
        tool_result_workflow_event = next(
            event
            for event in workflow_events
            if event.data.get("last_update", {}).get("message_type") == "tool_result"
        )

        assert tool_result_workflow_event.data["completed_count"] == 1
        assert tool_result_workflow_event.data["current_ac_index"] == 2
        last_update = tool_result_workflow_event.data["last_update"]
        assert last_update["message_type"] == "tool_result"
        assert last_update["content_preview"] == "Tool completed successfully."
        assert last_update["tool_name"] == "Edit"
        assert last_update["tool_input"] == {"file_path": "src/app.py"}
        assert last_update["tool_result"]["text_content"] == "[AC_COMPLETE: 1] Done!"
        assert last_update["tool_result"]["is_error"] is False
        assert last_update["tool_result"]["meta"] == {}
        assert last_update["tool_result"]["content"][0]["type"] == "text"
        assert last_update["tool_result"]["content"][0]["text"] == "[AC_COMPLETE: 1] Done!"
        assert last_update["runtime_signal"] == "tool_completed"
        assert last_update["runtime_status"] == "running"
        assert last_update["ac_tracking"] == {"started": [], "completed": [1]}

    @pytest.mark.asyncio
    async def test_execute_seed_failure(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Test handling of failed execution."""
        from ouroboros.core.types import Result

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(
                type="result",
                content="Task failed: connection error",
                data={"subtype": "error"},
            )

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_failed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_failed", mock_mark_failed):
                result = await runner.execute_seed(sample_seed)

        assert result.is_ok
        assert result.value.success is False
        assert "failed" in result.value.final_message.lower()

    @pytest.mark.asyncio
    async def test_execute_precreated_usage_limit_marks_session_paused(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Usage/quota window failures should pause instead of failing the session."""
        tracker = SessionTracker.create(
            "exec_usage_limit",
            sample_seed.metadata.seed_id,
            session_id="sess_usage_limit",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="codex_cli",
                    native_session_id="thread-usage-limit",
                ),
            )

        mock_adapter.execute_task = mock_execute
        mark_paused = AsyncMock(return_value=Result.ok(True))
        mark_failed = AsyncMock(return_value=Result.ok(None))

        with (
            patch.object(runner, "_register_session"),
            patch.object(runner, "_unregister_session"),
            patch.object(runner._session_repo, "mark_paused", mark_paused),
            patch.object(runner._session_repo, "mark_failed", mark_failed),
            patch.object(
                runner,
                "_report_frugality_retrospective",
                AsyncMock(return_value=False),
            ) as retrospective,
        ):
            result = await runner.execute_precreated_session(
                seed=sample_seed,
                tracker=tracker,
                parallel=False,
            )

        assert result.is_ok
        assert result.value.success is False
        mark_paused.assert_awaited_once()
        mark_failed.assert_not_called()

        pause_kwargs = mark_paused.await_args.kwargs
        assert pause_kwargs["pause_seconds"] == 18000
        assert pause_kwargs["pause_kind"] == "usage_limit"

        terminal_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.terminal"
        ]
        assert terminal_events
        terminal = terminal_events[-1]
        assert terminal.data["status"] == "paused"
        assert terminal.data["pause_seconds"] == 18000
        assert terminal.data["pause_kind"] == "usage_limit"
        retrospective.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_precreated_rejects_changed_seed_goal_before_effects(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Caller Seed mutations must fail before prompts, tools, or providers run."""
        tracker = SessionTracker.create(
            "exec_precreated_seed_drift",
            sample_seed.metadata.seed_id,
            session_id="sess_precreated_seed_drift",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        changed_seed = sample_seed.model_copy(update={"goal": f"{sample_seed.goal} changed"})
        provider = MagicMock(side_effect=AssertionError("provider must not run"))
        get_tools = AsyncMock(side_effect=AssertionError("tool setup must not run"))
        execute_parallel = AsyncMock(side_effect=AssertionError("parallel executor must not run"))
        persist_failure = AsyncMock(return_value=(None, None))
        mock_adapter.execute_task = provider

        try:
            with (
                patch(
                    "ouroboros.orchestrator.runner.build_system_prompt",
                    MagicMock(side_effect=AssertionError("system prompt must not be built")),
                ) as system_prompt,
                patch(
                    "ouroboros.orchestrator.runner.build_task_prompt",
                    MagicMock(side_effect=AssertionError("task prompt must not be built")),
                ) as task_prompt,
                patch.object(runner, "_get_merged_tools", get_tools),
                patch.object(runner, "_execute_parallel", execute_parallel),
                patch.object(runner, "_persist_failure_and_cleanup", persist_failure),
            ):
                result = await runner.execute_precreated_session(
                    seed=changed_seed,
                    tracker=tracker,
                    parallel=True,
                )

            assert result.is_err
            assert result.error.message == "Cannot resume with a modified Seed"
            system_prompt.assert_not_called()
            task_prompt.assert_not_called()
            get_tools.assert_not_awaited()
            execute_parallel.assert_not_awaited()
            provider.assert_not_called()
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )

    @pytest.mark.asyncio
    async def test_execute_precreated_rejects_stale_nested_input_proof_before_effects(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Valid nested input mutations need a matching durable fingerprint."""
        tracker = SessionTracker.create(
            "exec_precreated_input_drift",
            sample_seed.metadata.seed_id,
            session_id="sess_precreated_input_drift",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        stale_contract = copy.deepcopy(tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY])
        stale_contract["execution_inputs"]["strategy"]["task_prompt_suffix"] += (
            "\nPreserve this mutated but otherwise valid suffix."
        )
        assert stale_contract["frugality_proof"][
            "execution_inputs_fingerprint"
        ] != runner._execution_inputs_fingerprint(stale_contract["execution_inputs"])
        tracker = tracker.with_progress({EXECUTION_CONTRACT_PROGRESS_KEY: stale_contract})
        provider = MagicMock(side_effect=AssertionError("provider must not run"))
        get_tools = AsyncMock(side_effect=AssertionError("tool setup must not run"))
        execute_parallel = AsyncMock(side_effect=AssertionError("parallel executor must not run"))
        persist_failure = AsyncMock(return_value=(None, None))
        mock_adapter.execute_task = provider

        try:
            with (
                patch(
                    "ouroboros.orchestrator.runner.build_system_prompt",
                    MagicMock(side_effect=AssertionError("system prompt must not be built")),
                ) as system_prompt,
                patch(
                    "ouroboros.orchestrator.runner.build_task_prompt",
                    MagicMock(side_effect=AssertionError("task prompt must not be built")),
                ) as task_prompt,
                patch.object(runner, "_get_merged_tools", get_tools),
                patch.object(runner, "_execute_parallel", execute_parallel),
                patch.object(runner, "_persist_failure_and_cleanup", persist_failure),
            ):
                result = await runner.execute_precreated_session(
                    seed=sample_seed,
                    tracker=tracker,
                    parallel=True,
                )

            assert result.is_err
            assert (
                result.error.message
                == "Caller-supplied execution contract does not match the persisted prepared contract"
            )
            assert result.error.details["resume_blocked"] == "prepared_execution_contract_mismatch"
            system_prompt.assert_not_called()
            task_prompt.assert_not_called()
            get_tools.assert_not_awaited()
            execute_parallel.assert_not_awaited()
            provider.assert_not_called()
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )

    @pytest.mark.asyncio
    async def test_execute_precreated_preserves_exact_prepared_state_success(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """The exact prepared v9 state still reaches the existing execution path."""
        tracker = SessionTracker.create(
            "exec_precreated_exact",
            sample_seed.metadata.seed_id,
            session_id="sess_precreated_exact",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        expected: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        )

        try:
            with (
                patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
                patch.object(
                    runner,
                    "_restore_execution_contract_snapshot",
                    wraps=runner._restore_execution_contract_snapshot,
                ) as restore_contract,
                patch.object(
                    runner, "_execute_parallel", AsyncMock(return_value=expected)
                ) as execute,
            ):
                result = await runner.execute_precreated_session(
                    seed=sample_seed,
                    tracker=tracker,
                    parallel=True,
                )

            assert result is expected
            assert (
                execute.await_args.kwargs["execution_contract"]
                == tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY]
            )
            assert restore_contract.call_count == 2
            assert all(
                call.kwargs["prepared_live_execution"] is True
                for call in restore_contract.call_args_list
            )
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )

    @pytest.mark.asyncio
    async def test_execute_precreated_pause_persistence_failure_preserves_owner(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Sequential pause must not report success while durable state is still RUNNING."""
        from ouroboros.orchestrator.heartbeat import is_holder_alive

        tracker = SessionTracker.create(
            "exec_pause_pending",
            sample_seed.metadata.seed_id,
            session_id="sess_pause_pending",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
            )

        mock_adapter.execute_task = mock_execute
        mark_paused = AsyncMock(
            return_value=Result.err(PersistenceError("pause write unavailable"))
        )

        try:
            with patch.object(runner._session_repo, "mark_paused", mark_paused):
                result = await runner.execute_precreated_session(
                    seed=sample_seed,
                    tracker=tracker,
                    parallel=False,
                )

            assert result.is_err
            assert result.error.details["resume_blocked"] == "pause_persistence_pending"
            assert result.error.details["requested_status"] == SessionStatus.PAUSED.value
            assert runner._has_live_process_local_authority(
                tracker.session_id,
                tracker.execution_id,
                tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
            )
            assert is_holder_alive(tracker.session_id)
            assert tracker.execution_id not in runner.active_sessions
            terminal_events = [
                call.args[0]
                for call in mock_event_store.append.await_args_list
                if getattr(call.args[0], "type", None) == "execution.terminal"
            ]
            assert not terminal_events
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
            runner._unregister_session(tracker.execution_id, tracker.session_id)

    @pytest.mark.asyncio
    async def test_execute_precreated_mirrors_the_durable_terminal_cas_winner(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """A concurrent terminal winner must replace the requested completion mirror."""
        tracker = SessionTracker.create(
            "exec_terminal_cas_winner",
            sample_seed.metadata.seed_id,
            session_id="sess_terminal_cas_winner",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        cancelled_tracker = tracker.with_status(SessionStatus.CANCELLED)

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Completed after cancellation won",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        mark_completed = AsyncMock(return_value=Result.ok(False))

        with (
            patch.object(runner, "_register_session"),
            patch.object(runner, "_unregister_session"),
            patch.object(runner._session_repo, "mark_completed", mark_completed),
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(cancelled_tracker)),
            ),
            patch.object(
                runner,
                "_report_frugality_retrospective",
                AsyncMock(return_value=False),
            ),
        ):
            result = await runner.execute_precreated_session(
                seed=sample_seed,
                tracker=tracker,
                parallel=False,
            )

        assert result.is_ok
        assert result.value.success is False
        mark_completed.assert_awaited_once()
        terminal_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.terminal"
        ]
        assert terminal_events[-1].data["status"] == SessionStatus.CANCELLED.value
        assert not [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "orchestrator.session.completed"
        ]

    @pytest.mark.asyncio
    async def test_execute_seed_exception_marks_session_failed(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Unexpected execution exceptions should mark the session as failed."""
        from ouroboros.core.types import Result

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            if False:
                yield AgentMessage(type="assistant", content="never")
            raise RuntimeError("coordinator crash")

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        mark_failed = AsyncMock(return_value=Result.ok(None))

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_failed", mark_failed):
                result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_err
        assert "coordinator crash" in str(result.error)
        mark_failed.assert_awaited_once()
        assert mark_failed.await_args.args[1] == "coordinator crash"

    @pytest.mark.asyncio
    async def test_execute_seed_session_creation_fails(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Test handling when session creation fails."""
        from ouroboros.core.errors import PersistenceError
        from ouroboros.core.types import Result

        with patch.object(
            runner._session_repo,
            "create_session",
            return_value=Result.err(PersistenceError("DB error")),
        ):
            result = await runner.execute_seed(sample_seed)

        assert result.is_err
        assert "session" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_execute_seed_session_creation_failure_releases_workspace_lock(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        """Session creation errors should not leak an acquired workspace lock."""
        from ouroboros.core.errors import PersistenceError
        from ouroboros.core.types import Result

        task_workspace = _runtime_owned_task_workspace(mock_adapter, tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=task_workspace,
            fat_harness_mode=False,
        )

        with (
            patch.object(
                runner._session_repo,
                "create_session",
                return_value=Result.err(PersistenceError("DB error")),
            ),
            patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
        ):
            result = await runner.execute_seed(sample_seed)

        assert result.is_err
        release_lock_mock.assert_called_once_with(task_workspace.lock_path)

    @pytest.mark.asyncio
    async def test_execute_seed_start_event_failure_cleans_up_workspace_lock(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        """A terminal-write failure releases effects but preserves the live lease."""
        from ouroboros.core.types import Result

        task_workspace = _runtime_owned_task_workspace(mock_adapter, tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=task_workspace,
            fat_harness_mode=False,
        )
        _allow_mocked_precreated_durable_state(runner)
        tracker = SessionTracker.create(
            "exec_setup",
            sample_seed.metadata.seed_id,
            session_id="orch_setup",
        )

        with (
            patch.object(runner._session_repo, "create_session", return_value=Result.ok(tracker)),
            patch.object(
                runner._session_repo, "track_progress", AsyncMock(return_value=Result.ok(None))
            ),
            patch.object(
                runner._event_store,
                "append",
                AsyncMock(side_effect=RuntimeError("event append failed")),
            ),
            patch.object(runner, "_unregister_session") as unregister_mock,
            patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
        ):
            result = await runner.execute_seed(
                sample_seed,
                execution_id="exec_setup",
                session_id=tracker.session_id,
            )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "terminal_persistence_pending"
        unregister_mock.assert_called_once_with(
            "exec_setup",
            tracker.session_id,
            release_liveness_lease=False,
        )
        release_lock_mock.assert_called_once_with(task_workspace.lock_path)
        runner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id="exec_setup",
        )
        runner._unregister_session("exec_setup", tracker.session_id)

    @pytest.mark.asyncio
    async def test_execute_seed_tool_setup_failure_cleans_up_workspace_lock(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        """Merged-tool setup failures should release the workspace lock and unregister."""
        from ouroboros.core.types import Result

        task_workspace = _runtime_owned_task_workspace(mock_adapter, tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=task_workspace,
            fat_harness_mode=False,
        )
        _allow_mocked_precreated_durable_state(runner)
        tracker = SessionTracker.create(
            "exec_tools",
            sample_seed.metadata.seed_id,
            session_id="orch_tools",
        )

        with (
            patch.object(runner._session_repo, "create_session", return_value=Result.ok(tracker)),
            patch.object(runner, "_get_merged_tools", AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(runner, "_unregister_session") as unregister_mock,
            patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
        ):
            result = await runner.execute_seed(
                sample_seed,
                execution_id="exec_tools",
                session_id=tracker.session_id,
            )

        assert result.is_err
        unregister_mock.assert_called_once_with("exec_tools", tracker.session_id)
        release_lock_mock.assert_called_once_with(task_workspace.lock_path)

    @pytest.mark.asyncio
    async def test_resume_session_already_completed(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Test that resuming completed session fails."""
        from ouroboros.core.types import Result

        completed_tracker = SessionTracker.create("exec", "seed").with_status(
            SessionStatus.COMPLETED
        )

        with patch.object(
            runner._session_repo,
            "reconstruct_session",
            return_value=Result.ok(completed_tracker),
        ):
            result = await runner.resume_session("sess_123", sample_seed)

        assert result.is_err
        assert "terminal state" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_resume_session_already_completed_releases_workspace_lock(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        """Terminal resume attempts should not leak the acquired workspace lock."""
        from ouroboros.core.types import Result

        task_workspace = _runtime_owned_task_workspace(mock_adapter, tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=task_workspace,
            fat_harness_mode=False,
        )
        completed_tracker = SessionTracker.create("exec", "seed").with_status(
            SessionStatus.COMPLETED
        )

        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                return_value=Result.ok(completed_tracker),
            ),
            patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
        ):
            result = await runner.resume_session("sess_123", sample_seed)

        assert result.is_err
        release_lock_mock.assert_called_once_with(task_workspace.lock_path)

    @pytest.mark.asyncio
    async def test_resume_is_blocked_before_ungated_direct_execution(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        """Resume must not bypass typed evidence acceptance."""
        from ouroboros.core.types import Result

        task_workspace = _runtime_owned_task_workspace(mock_adapter, tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            fat_harness_mode=True,
            task_workspace=task_workspace,
        )
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.PAUSED
        )
        running_tracker = _attach_live_process_local_contract(
            runner,
            running_tracker,
            sample_seed,
            session_id="sess_resume",
        )
        try:
            with (
                patch.object(
                    runner._session_repo,
                    "reconstruct_session",
                    return_value=Result.ok(running_tracker),
                ),
                patch.object(runner, "_get_merged_tools", AsyncMock()) as get_merged_tools,
                patch.object(mock_adapter, "execute_task", AsyncMock()) as execute_task,
                patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
            ):
                result = await runner.resume_session("sess_resume", sample_seed)
        finally:
            runner._retire_process_local_authority(
                session_id="sess_resume",
                execution_id=running_tracker.execution_id,
            )

        assert result.is_err
        assert "Resume is blocked" in result.error.message
        assert "typed evidence plus verifier PASS" in result.error.message
        assert result.error.details["resume_blocked"] == "typed_evidence_gate_required"
        get_merged_tools.assert_not_called()
        execute_task.assert_not_called()
        release_lock_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_investment_seed_is_blocked_before_ungated_direct_execution(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        """Resume must fail closed until it can restore per-AC investment authority."""
        from ouroboros.core.types import Result

        investment_seed = sample_seed.model_copy(
            update={
                "acceptance_criteria": (
                    AcceptanceCriterionSpec(
                        description="Rotate production signing keys",
                        investment=InvestmentSpec(
                            difficulty="medium",
                            stakes="high",
                            provenance="declared",
                            confidence="high",
                        ),
                    ),
                )
            }
        )
        task_workspace = _runtime_owned_task_workspace(mock_adapter, tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=task_workspace,
        )
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.PAUSED
        )
        running_tracker = _attach_live_process_local_contract(
            runner,
            running_tracker,
            investment_seed,
            session_id="sess_resume",
        )

        try:
            with (
                patch.object(
                    runner._session_repo,
                    "reconstruct_session",
                    return_value=Result.ok(running_tracker),
                ),
                patch.object(runner, "_get_merged_tools", AsyncMock()) as get_merged_tools,
                patch.object(mock_adapter, "execute_task", AsyncMock()) as execute_task,
                patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
            ):
                result = await runner.resume_session("sess_resume", investment_seed)
        finally:
            runner._retire_process_local_authority(
                session_id="sess_resume",
                execution_id=running_tracker.execution_id,
            )

        assert result.is_err
        assert "Resume is blocked" in result.error.message
        assert "per-AC investment authority" in result.error.message
        assert result.error.details["resume_blocked"] == "investment_authority_required"
        get_merged_tools.assert_not_called()
        execute_task.assert_not_called()
        release_lock_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_session_tool_setup_failure_cleans_up_workspace_lock(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        """Resume setup failures should release the workspace lock and unregister."""
        from ouroboros.core.types import Result

        task_workspace = _runtime_owned_task_workspace(mock_adapter, tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_workspace=task_workspace,
            fat_harness_mode=False,
        )
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.PAUSED
        )
        running_tracker = _attach_live_process_local_contract(
            runner,
            running_tracker,
            sample_seed,
            session_id="sess_resume_tool_setup",
        )

        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                return_value=Result.ok(running_tracker),
            ),
            patch.object(runner, "_get_merged_tools", AsyncMock(side_effect=RuntimeError("boom"))),
            patch.object(runner, "_unregister_session") as unregister_mock,
            patch("ouroboros.orchestrator.runner.release_lock") as release_lock_mock,
        ):
            result = await runner.resume_session("sess_resume_tool_setup", sample_seed)

        assert result.is_err
        unregister_mock.assert_called_once_with("exec_resume", "sess_resume_tool_setup")
        release_lock_mock.assert_called_once_with(task_workspace.lock_path)

    @pytest.mark.asyncio
    async def test_resume_session_not_found(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Test handling when session not found."""
        from ouroboros.core.errors import PersistenceError
        from ouroboros.core.types import Result

        with patch.object(
            runner._session_repo,
            "reconstruct_session",
            return_value=Result.err(PersistenceError("Session not found")),
        ):
            result = await runner.resume_session("nonexistent", sample_seed)

        assert result.is_err

    @pytest.mark.asyncio
    async def test_resume_session_recoverable_failure_marks_session_paused(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Recoverable resume bootstrap failures should not poison the session."""
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.PAUSED
        )
        running_tracker = _attach_live_process_local_contract(
            runner,
            running_tracker,
            sample_seed,
            session_id="sess_resume_recoverable",
        )
        terminate_calls = 0

        async def terminate(_handle: RuntimeHandle) -> bool:
            nonlocal terminate_calls
            terminate_calls += 1
            return True

        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
        ).bind_controls(terminate_callback=terminate)

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Codex rejected the resume command",
                data={
                    "subtype": "error",
                    "recoverable": True,
                    "recovery": {"kind": "resume_retry"},
                },
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(running_tracker)),
            ),
            patch.object(
                runner._session_repo,
                "mark_paused",
                AsyncMock(return_value=Result.ok(True)),
            ) as mark_paused,
            patch.object(
                runner._session_repo,
                "mark_failed",
                AsyncMock(return_value=Result.ok(None)),
            ) as mark_failed,
            patch.object(
                runner,
                "_report_frugality_retrospective",
                AsyncMock(return_value=False),
            ) as retrospective,
        ):
            result = await runner.resume_session("sess_resume_recoverable", sample_seed)

        assert result.is_ok
        assert result.value.success is False
        assert result.value.final_message == "Codex rejected the resume command"
        assert terminate_calls == 0
        mark_paused.assert_awaited_once()
        mark_failed.assert_not_called()
        retrospective.assert_not_awaited()
        runner._retire_process_local_authority(
            session_id="sess_resume_recoverable",
            execution_id=running_tracker.execution_id,
        )

    @pytest.mark.asyncio
    async def test_resume_pause_persistence_failure_preserves_owner(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Resume pause persistence failure must not masquerade as a PAUSED result."""
        from ouroboros.orchestrator.heartbeat import is_holder_alive

        tracker = SessionTracker.create(
            "exec_resume_pending",
            "seed_resume",
            session_id="sess_resume_pause_pending",
        ).with_status(
            SessionStatus.PAUSED,
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id="sess_resume_pause_pending",
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Codex rejected the resume command",
                data={
                    "subtype": "error",
                    "recoverable": True,
                    "recovery": {"kind": "resume_retry"},
                },
            )

        mock_adapter.execute_task = mock_execute

        try:
            with (
                patch.object(
                    runner._session_repo,
                    "reconstruct_session",
                    AsyncMock(return_value=Result.ok(tracker)),
                ),
                patch.object(
                    runner._session_repo,
                    "mark_paused",
                    AsyncMock(return_value=Result.err(PersistenceError("pause write unavailable"))),
                ),
            ):
                result = await runner.resume_session(tracker.session_id, sample_seed)

            assert result.is_err
            assert result.error.details["resume_blocked"] == "pause_persistence_pending"
            assert runner._has_live_process_local_authority(
                tracker.session_id,
                tracker.execution_id,
                tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
            )
            assert is_holder_alive(tracker.session_id)
            assert tracker.execution_id not in runner.active_sessions
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
            runner._unregister_session(tracker.execution_id, tracker.session_id)

    @pytest.mark.asyncio
    async def test_resume_paused_projection_failure_preserves_owner(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Resume auxiliary failure after PAUSED cannot enter generic FAILED cleanup."""
        from ouroboros.orchestrator.heartbeat import is_holder_alive

        tracker = SessionTracker.create(
            "exec_resume_projection",
            "seed_resume",
            session_id="sess_resume_projection",
        ).with_status(SessionStatus.PAUSED)
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Codex rejected the resume command",
                data={
                    "subtype": "error",
                    "recoverable": True,
                    "recovery": {"kind": "resume_retry"},
                },
            )

        async def append(event: BaseEvent) -> None:
            if event.type == "execution.terminal":
                raise PersistenceError("execution projection unavailable")

        mock_adapter.execute_task = mock_execute
        mock_event_store.append.side_effect = append
        mark_failed = AsyncMock(return_value=Result.ok(True))

        try:
            with (
                patch.object(
                    runner._session_repo,
                    "reconstruct_session",
                    AsyncMock(return_value=Result.ok(tracker)),
                ),
                patch.object(
                    runner._session_repo,
                    "mark_paused",
                    AsyncMock(return_value=Result.ok(True)),
                ),
                patch.object(runner._session_repo, "mark_failed", mark_failed),
            ):
                result = await runner.resume_session(tracker.session_id, sample_seed)

            assert result.is_ok
            assert result.value.success is False
            mark_failed.assert_not_awaited()
            assert runner._has_live_process_local_authority(
                tracker.session_id,
                tracker.execution_id,
                tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
            )
            assert is_holder_alive(tracker.session_id)
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
            runner._unregister_session(tracker.execution_id, tracker.session_id)

    @pytest.mark.asyncio
    async def test_resume_pause_preserves_late_cancellation_marker(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """A cancellation arriving during resumed pause publication is not acknowledged."""
        tracker = SessionTracker.create(
            "exec_resume_cancel",
            "seed_resume",
            session_id="sess_resume_pause_cancel",
        ).with_status(
            SessionStatus.PAUSED,
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id="sess_resume_pause_cancel",
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Codex rejected the resume command",
                data={
                    "subtype": "error",
                    "recoverable": True,
                    "recovery": {"kind": "resume_retry"},
                },
            )

        async def mark_paused(*args: Any, **kwargs: Any) -> Result[bool, PersistenceError]:
            del args, kwargs
            await request_cancellation(tracker.session_id)
            return Result.ok(True)

        mock_adapter.execute_task = mock_execute

        try:
            with (
                patch.object(
                    runner._session_repo,
                    "reconstruct_session",
                    AsyncMock(return_value=Result.ok(tracker)),
                ),
                patch.object(runner._session_repo, "mark_paused", mark_paused),
            ):
                result = await runner.resume_session(tracker.session_id, sample_seed)

            assert result.is_ok
            assert await is_cancellation_requested(tracker.session_id)
        finally:
            await clear_cancellation(tracker.session_id)
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
            runner._unregister_session(tracker.execution_id, tracker.session_id)

    def test_recoverable_failure_ignores_ordinary_429(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Plain 429/rate-limit errors should not trigger a long usage pause."""
        message = AgentMessage(
            type="result",
            content="429 Too Many Requests: rate limit exceeded",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )

        pause = runner._recoverable_failure_pause(
            message,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )

        assert pause is None

    def test_recoverable_failure_ignores_task_text_about_usage_limits(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Task failures that merely mention usage limits are not provider quota pauses."""
        message = AgentMessage(
            type="result",
            content="Tests failed while updating usage limit copy. Try again in 5 hours.",
            data={"subtype": "error"},
        )

        pause = runner._recoverable_failure_pause(
            message,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )

        assert pause is None

    def test_recoverable_failure_detects_usage_limit_window(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Provider/runtime quota-window errors should become paused sessions."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        message = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )

        pause = runner._recoverable_failure_pause(
            message,
            now=now,
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.pause_kind == "usage_limit"
        assert pause.pause_seconds == 18000
        assert pause.resume_after == now + timedelta(hours=5)

    def test_recoverable_failure_bounds_deep_plain_metadata_before_resume_retry(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """The runner must not recurse through a provider-controlled envelope."""
        data: dict[str, object] = {"subtype": "error"}
        cursor = data
        for _ in range(1_200):
            nested: dict[str, object] = {}
            cursor["error"] = nested
            cursor = nested
        message = AgentMessage(
            type="result",
            content="Provider returned malformed nested metadata",
            data=data,
        )

        pause = runner._recoverable_failure_pause(
            message,
            now=datetime(2026, 1, 1, tzinfo=UTC),
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.pause_kind == "usage_limit"

    def test_recoverable_failure_detects_hermes_usage_limit_exit(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Regression for issue 1.1: a hermes non-zero exit carrying a Z.AI/GLM
        usage-limit 429 must pause, not hard-fail. Before the typed-error
        contract the hermes path emitted only ``{"subtype","exit_code"}``, which
        the classifier rejected for lacking runtime-error shape."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        failure_text = (
            "API call failed after 3 retries:\n"
            "HTTP 429: Usage limit reached for 5 hour.\n"
            "Your limit will reset at 2026-06-07 21:41:18"
        )
        message = AgentMessage(
            type="result",
            content=f"Hermes execution failed:\n{failure_text}",
            data=classify_subprocess_failure(failure_text, exit_code=1),
        )

        pause = runner._recoverable_failure_pause(
            message,
            now=now,
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.pause_kind == "usage_limit"
        assert pause.pause_seconds == 18000
        assert pause.resume_after == now + timedelta(hours=5)

    def test_recoverable_failure_ignores_ordinary_hermes_exit(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """A typed-but-ordinary hermes failure must not trigger a usage pause."""
        message = AgentMessage(
            type="result",
            content="Hermes execution failed:\nTraceback: ValueError: bad config",
            data=classify_subprocess_failure(
                "Traceback: ValueError: bad config",
                exit_code=1,
            ),
        )

        pause = runner._recoverable_failure_pause(
            message,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )

        assert pause is None

    def test_recoverable_failure_ignores_hermes_task_text_about_usage_limits(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Hermes task text mentioning usage-limit copy is not a provider pause."""
        failure_text = (
            "Tests failed while updating usage limit copy. "
            "Please try again in 5 hours after the flaky suite is fixed."
        )
        message = AgentMessage(
            type="result",
            content=f"Hermes execution failed:\n{failure_text}",
            data=classify_subprocess_failure(failure_text, exit_code=1),
        )

        pause = runner._recoverable_failure_pause(
            message,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )

        assert pause is None

    def test_recoverable_failure_sums_compound_retry_window(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Compound quota windows should not resume before the full retry duration."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        message = AgentMessage(
            type="result",
            content="Usage limit reached. Please retry after 1 hour 30 minutes.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )

        pause = runner._recoverable_failure_pause(
            message,
            now=now,
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.pause_seconds == 5400
        assert pause.resume_after == now + timedelta(hours=1, minutes=30)
        assert OrchestratorRunner._duration_text_to_seconds("resets in 2h 15m") == 8100

    @pytest.mark.parametrize(
        ("metadata", "expected_seconds"),
        [
            ({"retry_after_ms": 1500}, 2),
            ({"retryAfterMs": "1500"}, 2),
            ({"retry_after": "2026-01-01T00:00:01.900000+00:00"}, 2),
            ({"resume_after": "2026-01-01T00:00:01.900000+00:00"}, 2),
            ({"retry_after_seconds": 1.1}, 2),
        ],
    )
    def test_recoverable_failure_rounds_retry_windows_up(
        self,
        metadata: dict[str, object],
        expected_seconds: int,
    ) -> None:
        """Sub-second retry hints must not resume before the provider boundary."""
        now = datetime(2026, 1, 1, tzinfo=UTC)

        assert OrchestratorRunner._duration_from_metadata(metadata, now=now) == expected_seconds

    @pytest.mark.parametrize(
        "retry_after",
        [
            float("inf"),
            float("-inf"),
            float("nan"),
            "inf",
            "-inf",
            "nan",
            f"{'9' * 400} hours",
        ],
    )
    def test_recoverable_failure_ignores_non_finite_retry_metadata(
        self,
        retry_after: object,
    ) -> None:
        """Untrusted retry metadata must keep quota classification non-throwing."""
        now = datetime(2026, 1, 1, tzinfo=UTC)

        assert (
            OrchestratorRunner._duration_from_metadata(
                {"retry_after_seconds": retry_after},
                now=now,
            )
            is None
        )

    def test_recoverable_failure_handles_huge_integer_retry_metadata(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        huge_retry = 10**1000
        now = datetime(2026, 1, 1, tzinfo=UTC)

        assert (
            OrchestratorRunner._duration_from_metadata(
                {"retry_after_seconds": huge_retry},
                now=now,
            )
            == huge_retry
        )
        assert (
            OrchestratorRunner._duration_from_metadata(
                {"retry_after_ms": huge_retry},
                now=now,
            )
            == (huge_retry + 999) // 1000
        )

        message = AgentMessage(
            type="result",
            content="Usage limit reached.",
            data={
                "subtype": "error",
                "error_type": "UsageLimitError",
                "retry_after_seconds": huge_retry,
            },
        )
        pause = runner._recoverable_failure_pause(
            message,
            now=now,
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.pause_seconds == 18000
        assert pause.resume_after == now + timedelta(hours=5)

    def test_recoverable_failure_requires_durable_usage_limit_policy(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Pause construction cannot reread a live fallback after provider effect."""
        message = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again later.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )

        with pytest.raises(ConfigError):
            runner._recoverable_failure_pause(
                message,
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_parallel_result_detects_nested_usage_limit_window(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Parallel AC failures should also pause on provider quota windows."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        message = AgentMessage(
            type="result",
            content="Quota window exhausted. Retry after 2 hours.",
            data={"subtype": "error", "error_type": "OpenCodeError"},
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Patch the runner",
                    success=False,
                    messages=(message,),
                    final_message=message.content,
                ),
            ),
            success_count=0,
            failure_count=1,
            total_messages=1,
        )

        pause = runner._recoverable_failure_pause_from_parallel_result(
            parallel_result,
            now=now,
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.pause_kind == "usage_limit"
        assert pause.pause_seconds == 7200
        assert pause.resume_after == now + timedelta(hours=2)

    def test_parallel_result_detects_decomposed_usage_limit_window(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Decomposed AC parents should defer to recoverable failed leaves."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        message = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 3 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        child_result = ACExecutionResult(
            ac_index=100,
            ac_content="Patch the runner leaf",
            success=False,
            messages=(message,),
            final_message=message.content,
        )
        parent_result = ACExecutionResult(
            ac_index=0,
            ac_content="Patch the runner",
            success=False,
            messages=(),
            is_decomposed=True,
            sub_results=(child_result,),
        )
        parallel_result = ParallelExecutionResult(
            results=(parent_result,),
            success_count=0,
            failure_count=1,
            total_messages=1,
        )

        pause = runner._recoverable_failure_pause_from_parallel_result(
            parallel_result,
            now=now,
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.pause_kind == "usage_limit"
        assert pause.resume_after == now + timedelta(hours=3)

    def test_parallel_result_detects_coordinator_usage_limit_window(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Coordinator quota evidence must reach the durable runner pause owner."""

        now = datetime(2026, 1, 1, tzinfo=UTC)
        message = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 4 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        successful = ACExecutionResult(
            ac_index=0,
            ac_content="Patch shared.py",
            success=True,
            final_message="done",
        )
        consequence = UsageLimitPauseConsequence(
            reason=message.content,
            resume_hint=(
                "Provider usage/quota window reached. Resume after "
                "2026-01-01T04:00:00+00:00 (wait at least 4 hours)."
            ),
            pause_seconds=14_400,
            resume_after=now + timedelta(hours=4),
        )
        coordinator_pause = CoordinatorQuotaPause(
            execution_id="execution-coordinator-pause",
            session_id="session-coordinator-pause",
            level_number=1,
            coordinator_aggregate_id="execution-coordinator-pause:l0:coord",
            consequence=consequence,
        )
        parallel_result = ParallelExecutionResult(
            results=(successful,),
            success_count=1,
            failure_count=0,
            stages=(
                ParallelExecutionStageResult(
                    stage_index=0,
                    ac_indices=(0,),
                    results=(successful,),
                    coordinator_review=CoordinatorReview(
                        level_number=1,
                        review_summary=message.content,
                        recoverable_quota_pause=consequence,
                    ),
                ),
            ),
            recoverable_coordinator_pause=coordinator_pause,
        )

        pause = runner._recoverable_failure_pause_from_parallel_result(
            parallel_result,
            now=now,
            default_pause_seconds=18_000,
        )

        assert parallel_result.all_succeeded is False
        assert pause is not None
        assert pause.pause_kind == "usage_limit"
        assert pause.resume_after == now + timedelta(hours=4)
        assert pause.coordinator_owner == coordinator_pause

    @pytest.mark.asyncio
    async def test_runner_publishes_and_consumes_exact_restored_coordinator_pause(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Crash replay publishes PAUSED before its exact owner may continue."""

        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create(
            "execution-coordinator-crash-replay",
            sample_seed.metadata.seed_id,
            session_id="session-coordinator-crash-replay",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        resume_after = datetime(2026, 1, 1, 5, tzinfo=UTC)
        consequence = UsageLimitPauseConsequence(
            reason="Usage limit reached. Please try again in 5 hours.",
            resume_hint=(
                "Provider usage/quota window reached. Resume after "
                "2026-01-01T05:00:00+00:00 (wait at least 5 hours)."
            ),
            pause_seconds=18_000,
            resume_after=resume_after,
        )
        coordinator_pause = CoordinatorQuotaPause(
            execution_id=tracker.execution_id,
            session_id=tracker.session_id,
            level_number=1,
            coordinator_aggregate_id=f"{tracker.execution_id}:l0:coord",
            consequence=consequence,
        )
        first_ac = ACExecutionResult(
            ac_index=0,
            ac_content=sample_seed.acceptance_criteria[0],
            success=True,
            final_message="stage one complete",
        )
        restored_pause_result = ParallelExecutionResult(
            results=(first_ac,),
            success_count=1,
            failure_count=0,
            recoverable_coordinator_pause=coordinator_pause,
        )
        completed_results = tuple(
            ACExecutionResult(
                ac_index=index,
                ac_content=content,
                success=True,
                final_message="done",
            )
            for index, content in enumerate(sample_seed.acceptance_criteria)
        )
        completed = ParallelExecutionResult(
            results=completed_results,
            success_count=len(completed_results),
            failure_count=0,
        )
        execute_parallel = AsyncMock(side_effect=(restored_pause_result, completed))
        mark_paused = AsyncMock(return_value=Result.ok(True))
        tool_catalog = assemble_session_tool_catalog(["Read"])

        with (
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                execute_parallel,
            ),
            patch.object(runner._session_repo, "mark_paused", mark_paused),
            patch.object(
                runner,
                "_persist_session_terminal_status",
                AsyncMock(return_value=SessionStatus.COMPLETED),
            ),
            patch.object(runner, "_build_terminal_acceptance_finalizations", return_value=[]),
            patch.object(runner, "_report_frugality_retrospective", AsyncMock(return_value=False)),
        ):
            crash_recovery = await runner._execute_parallel(
                seed=sample_seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=tool_catalog,
                system_prompt="system",
                start_time=tracker.start_time,
                execution_contract=tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
                force_sequential_levels=True,
            )

            assert crash_recovery.is_ok
            assert crash_recovery.value.success is False
            owner_payload = coordinator_pause.owner_payload()
            assert mark_paused.await_args.kwargs["pause_owner"] == owner_payload

            paused_tracker = tracker.with_status(SessionStatus.PAUSED).with_progress(
                {"pause_owner": owner_payload}
            )
            resumed = await runner._execute_parallel(
                seed=sample_seed,
                exec_id=paused_tracker.execution_id,
                tracker=paused_tracker,
                merged_tools=["Read"],
                tool_catalog=tool_catalog,
                system_prompt="system",
                start_time=paused_tracker.start_time,
                execution_contract=paused_tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
                force_sequential_levels=True,
            )

        assert resumed.is_ok
        assert resumed.value.success is True
        assert execute_parallel.await_count == 2
        assert (
            execute_parallel.await_args_list[0].kwargs["published_coordinator_pause_owner"] is None
        )
        assert (
            execute_parallel.await_args_list[1].kwargs["published_coordinator_pause_owner"]
            == coordinator_pause.owner_payload()
        )

    def test_parallel_result_requires_all_failures_recoverable(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Mixed quota and ordinary failures should remain failed, not paused."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        usage_message = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        ordinary_message = AgentMessage(
            type="result",
            content="Tests failed: expected 2 rows, got 1.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Patch the runner",
                    success=False,
                    messages=(usage_message,),
                    final_message=usage_message.content,
                ),
                ACExecutionResult(
                    ac_index=1,
                    ac_content="Patch the tests",
                    success=False,
                    messages=(ordinary_message,),
                    final_message=ordinary_message.content,
                ),
            ),
            success_count=0,
            failure_count=2,
            total_messages=2,
        )

        pause = runner._recoverable_failure_pause_from_parallel_result(
            parallel_result,
            now=now,
            default_pause_seconds=18_000,
        )

        assert pause is None

    def test_bounded_route_pause_preserves_mixed_failure_round_for_resume(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """A typed route pause owns the round even with a pending sibling route."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        usage_message = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        ordinary_message = AgentMessage(
            type="result",
            content="Tests failed: expected 2 rows, got 1.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Patch the runner",
                    success=False,
                    messages=(usage_message,),
                    outcome=ACExecutionOutcome.BLOCKED,
                ),
                ACExecutionResult(
                    ac_index=1,
                    ac_content="Patch the tests",
                    success=False,
                    messages=(ordinary_message,),
                ),
            ),
            success_count=0,
            failure_count=2,
            recoverable_route_pause=True,
        )

        pause = runner._recoverable_failure_pause_from_parallel_result(
            parallel_result,
            now=now,
            require_all_failures_recoverable=False,
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.resume_after == now + timedelta(hours=5)

    def test_parallel_result_uses_latest_recoverable_resume_after(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """All-recoverable parallel failures should wait for the longest window."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        two_hour_message = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 2 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        five_hour_message = AgentMessage(
            type="result",
            content="Quota window exhausted. Retry after 5 hours.",
            data={"subtype": "error", "error_type": "OpenCodeError"},
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Patch the runner",
                    success=False,
                    messages=(two_hour_message,),
                    final_message=two_hour_message.content,
                ),
                ACExecutionResult(
                    ac_index=1,
                    ac_content="Patch the tests",
                    success=False,
                    messages=(five_hour_message,),
                    final_message=five_hour_message.content,
                ),
            ),
            success_count=0,
            failure_count=2,
            total_messages=2,
        )

        pause = runner._recoverable_failure_pause_from_parallel_result(
            parallel_result,
            now=now,
            default_pause_seconds=18_000,
        )

        assert pause is not None
        assert pause.pause_seconds == 18000
        assert pause.resume_after == now + timedelta(hours=5)

    @pytest.mark.asyncio
    async def test_parallel_pause_persistence_failure_preserves_owner(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Parallel pause persistence failure must remain typed and retryable."""
        from ouroboros.orchestrator.heartbeat import is_holder_alive
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create(
            "exec_parallel_pause_pending",
            sample_seed.metadata.seed_id,
            session_id="sess_parallel_pause_pending",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
        assert generation is not None
        assert already_claimed is False
        runner._register_session(tracker.execution_id, tracker.session_id)
        message = AgentMessage(
            type="result",
            content="Quota window exhausted. Retry after 2 hours.",
            data={"subtype": "error", "error_type": "OpenCodeError"},
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=False,
                    messages=(message,),
                    final_message=message.content,
                ),
            ),
            success_count=0,
            failure_count=1,
            total_messages=1,
        )

        try:
            with (
                patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
                patch(
                    "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                    AsyncMock(return_value=parallel_result),
                ),
                patch.object(
                    runner._session_repo,
                    "mark_paused",
                    AsyncMock(return_value=Result.err(PersistenceError("pause write unavailable"))),
                ),
            ):
                result = await runner._execute_parallel(
                    seed=sample_seed,
                    exec_id=tracker.execution_id,
                    tracker=tracker,
                    merged_tools=["Read"],
                    tool_catalog=assemble_session_tool_catalog(["Read"]),
                    system_prompt="system",
                    start_time=tracker.start_time,
                    force_sequential_levels=True,
                )

            assert result.is_err
            assert result.error.details["resume_blocked"] == "pause_persistence_pending"
            assert runner._has_live_process_local_authority(
                tracker.session_id,
                tracker.execution_id,
                tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
            )
            assert is_holder_alive(tracker.session_id)
            assert tracker.execution_id not in runner.active_sessions
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
            runner._unregister_session(tracker.execution_id, tracker.session_id)

    @pytest.mark.asyncio
    async def test_parallel_owner_publication_failure_prevents_executor_effects(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """One pre-await Routing D snapshot owns executor and owner publication."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create(
            "exec_parallel_owner_first",
            sample_seed.metadata.seed_id,
            session_id="sess_parallel_owner_first",
        )
        _enable_direct_bounded_routes(runner, runner._adapter)

        async def drift_capability_after_snapshot(*_args: Any, **_kwargs: Any) -> bool:
            runner._adapter.capabilities = RuntimeCapabilities(
                skill_dispatch=True,
                targeted_resume=True,
                structured_output=True,
                model_override_support=ParamSupport.IGNORED,
            )
            return False

        execute_parallel = AsyncMock(
            side_effect=AssertionError("parallel executor entered before owner publication")
        )
        with (
            patch.object(
                runner,
                "_check_cancellation",
                AsyncMock(side_effect=drift_capability_after_snapshot),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                execute_parallel,
            ),
            patch.object(
                runner._session_repo,
                "track_progress",
                AsyncMock(return_value=Result.err(PersistenceError("owner store unavailable"))),
            ),
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
                force_sequential_levels=True,
            )

        assert result.is_err
        assert result.error.message == "Failed to persist the parallel Routing D resume owner"
        execute_parallel.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lost_prerequisite",
        ["model_router", "route_economics", "model_enforcement", "durable_depth"],
    )
    async def test_persisted_parallel_owner_rejects_lost_model_enforcement_before_executor(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
        lost_prerequisite: str,
    ) -> None:
        """The owner marker closes every pre-first-route-event activation gap."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        _enable_direct_bounded_routes(runner, runner._adapter)
        tracker = SessionTracker.create(
            "exec_parallel_owner_capability_loss",
            sample_seed.metadata.seed_id,
            session_id="sess_parallel_owner_capability_loss",
        ).with_progress({"routing_resume_owner": "parallel"})
        if lost_prerequisite == "model_router":
            runner._model_router = None
        elif lost_prerequisite == "route_economics":
            runner._route_economics = None
        elif lost_prerequisite == "model_enforcement":
            runner._adapter.capabilities = RuntimeCapabilities(
                skill_dispatch=True,
                targeted_resume=True,
                structured_output=True,
                model_override_support=ParamSupport.IGNORED,
            )
        else:
            runner._max_decomposition_depth = MAX_DECOMPOSITION_DEPTH + 1
        assert runner._bounded_route_runtime_active() is False
        executor_cls = MagicMock(
            side_effect=AssertionError("parallel executor entered without model enforcement")
        )
        analyzer = MagicMock()
        analyzer.analyze = AsyncMock(
            side_effect=AssertionError("dependency analyzer entered before owner enforcement")
        )
        runner._build_dependency_analyzer = MagicMock(return_value=analyzer)  # type: ignore[method-assign]

        with patch(
            "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
            executor_cls,
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_err
        assert result.error.details["resume_blocked"] == "routing_enforcement_unavailable"
        assert result.error.details["retryable"] is True
        analyzer.analyze.assert_not_awaited()
        executor_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_parallel_dispatch_rejects_post_restore_execution_semantics_drift(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """The invocation snapshot stays authoritative after restore returns."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create(
            "exec_parallel_semantics_drift",
            sample_seed.metadata.seed_id,
            session_id="sess_parallel_semantics_drift",
        )
        execution_contract = runner._build_execution_contract(
            project_identity=runner._project_identity(), seed=sample_seed
        )
        runner._run_verify_commands = not runner._run_verify_commands
        analyzer = MagicMock()
        analyzer.analyze = AsyncMock(
            side_effect=AssertionError("dependency analyzer entered after semantics drift")
        )
        runner._build_dependency_analyzer = MagicMock(return_value=analyzer)  # type: ignore[method-assign]
        executor_cls = MagicMock(
            side_effect=AssertionError("parallel executor entered after semantics drift")
        )

        with patch(
            "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
            executor_cls,
        ):
            with pytest.raises(OrchestratorError, match="execution semantics drifted"):
                await runner._execute_parallel(
                    seed=sample_seed,
                    exec_id=tracker.execution_id,
                    tracker=tracker,
                    merged_tools=["Read"],
                    tool_catalog=assemble_session_tool_catalog(["Read"]),
                    system_prompt="system",
                    start_time=tracker.start_time,
                    execution_contract=execution_contract,
                )

        analyzer.analyze.assert_not_awaited()
        executor_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_provider_entry_rejects_post_restore_runtime_capability_drift(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
    ) -> None:
        """The provider choke point rechecks the full durable capability vocabulary."""
        _enable_direct_bounded_routes(runner, mock_adapter)
        runner._reasoning_effort = "high"
        mock_adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            reasoning_effort_support=ParamSupport.NATIVE,
            enforceable_reasoning_efforts=frozenset({"low", "medium", "high", "xhigh"}),
            model_override_support=ParamSupport.NATIVE,
        )
        expected = runner._execution_semantics_contract()["runtime_effect_capabilities"]
        assert isinstance(expected, dict)

        mock_adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            reasoning_effort_support=ParamSupport.IGNORED,
            enforceable_reasoning_efforts=frozenset({"low", "medium", "high", "xhigh"}),
            model_override_support=ParamSupport.NATIVE,
        )

        with pytest.raises(OrchestratorError, match="capabilities drifted before dispatch"):
            await runner._route_call_effort(
                execution_id="execution-capability-drift",
                session_id="session-capability-drift",
                bounded_escalation=True,
                expected_runtime_effect_capabilities=expected,
            )

    @pytest.mark.asyncio
    async def test_parallel_dispatch_consumes_resolved_worker_and_rate_snapshot(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """No live backend-limit reread may replace the persisted executor inputs."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create(
            "exec_parallel_resolved_limits",
            sample_seed.metadata.seed_id,
            session_id="sess_parallel_resolved_limits",
        )
        execution_contract = runner._build_execution_contract(
            project_identity=runner._project_identity(), seed=sample_seed
        )
        semantics = execution_contract["execution_semantics"]
        executor_cls = MagicMock()
        cancellation_result: Result[OrchestratorResult, OrchestratorError] = Result.err(
            OrchestratorError(message="cancelled after constructor inspection")
        )

        with (
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                executor_cls,
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=True)),
            patch.object(
                runner,
                "_handle_cancellation",
                AsyncMock(return_value=cancellation_result),
            ),
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
                execution_contract=execution_contract,
                force_sequential_levels=True,
            )

        assert result is cancellation_result
        kwargs = executor_cls.call_args.kwargs
        assert kwargs["max_concurrent"] == semantics["effective_parallel_workers"]
        assert kwargs["adaptive_max_concurrent"] == semantics["max_parallel_workers"]
        assert (
            kwargs["resolved_self_governs_rate_limit"]
            is semantics["backend_self_governs_rate_limit"]
        )
        limits = kwargs["resolved_backend_limits"]
        assert limits.backend == semantics["backend_limits_backend"]
        assert limits.max_concurrency == semantics["backend_max_concurrency"]
        assert limits.requests_per_minute == semantics["backend_requests_per_minute"]
        assert limits.tokens_per_minute == semantics["backend_tokens_per_minute"]

    @pytest.mark.asyncio
    async def test_resume_session_enforces_owner_marker_before_provider_when_capability_is_lost(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Production resume dispatch cannot fall through during the empty-event crash window."""

        _enable_direct_bounded_routes(runner, runner._adapter)
        tracker = SessionTracker.create(
            "exec_parallel_owner_resume_capability_loss",
            sample_seed.metadata.seed_id,
            session_id="sess_parallel_owner_resume_capability_loss",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        graph = DependencyGraph(
            nodes=tuple(
                ACNode(index=index, content=criterion)
                for index, criterion in enumerate(sample_seed.acceptance_criteria)
            ),
            execution_levels=tuple(
                (index,) for index in range(len(sample_seed.acceptance_criteria))
            ),
        )
        tracker = tracker.with_status(SessionStatus.PAUSED).with_progress(
            {
                "routing_resume_owner": "parallel",
                "routing_parallel_force_sequential": False,
                "routing_parallel_plan": runner._serialize_parallel_resume_plan(
                    graph.to_execution_plan()
                ),
                "routing_parallel_externally_satisfied_acs": {},
            }
        )
        runner._adapter.capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            model_override_support=ParamSupport.IGNORED,
        )
        provider = MagicMock(side_effect=AssertionError("provider entered after capability loss"))
        runner._adapter.execute_task = provider

        with patch.object(
            runner._session_repo,
            "reconstruct_session",
            AsyncMock(return_value=Result.ok(tracker)),
        ):
            result = await runner.resume_session(tracker.session_id, sample_seed)

        assert result.is_err
        assert result.error.details["resume_blocked"] == "runtime_effect_capability_drift"
        assert result.error.details["retryable"] is True
        provider.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("compatibility_mode", ["routing_dormant", "depth_above_durable"])
    async def test_legacy_parallel_pause_does_not_publish_routing_d_resume_owner(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
        compatibility_mode: str,
    ) -> None:
        """Dormant Routing D cannot claim replay authority for legacy stages."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        if compatibility_mode == "routing_dormant":
            runner._model_router = None
        else:
            runner._max_decomposition_depth = MAX_DECOMPOSITION_DEPTH + 1
        tracker = SessionTracker.create(
            "exec_legacy_parallel_pause",
            sample_seed.metadata.seed_id,
            session_id="sess_legacy_parallel_pause",
        )
        graph = DependencyGraph(
            nodes=tuple(
                ACNode(index=index, content=criterion)
                for index, criterion in enumerate(sample_seed.acceptance_criteria)
            ),
            execution_levels=tuple(
                (index,) for index in range(len(sample_seed.acceptance_criteria))
            ),
        )
        quota_message = AgentMessage(
            type="result",
            content="Usage limit reached. Retry after 2 hours.",
            data={"subtype": "error", "error_type": "UsageLimitError"},
        )
        legacy_pause = ParallelExecutionResult(
            results=tuple(
                ACExecutionResult(
                    ac_index=index,
                    ac_content=criterion,
                    success=False,
                    messages=(quota_message,),
                    final_message=quota_message.content,
                )
                for index, criterion in enumerate(sample_seed.acceptance_criteria)
            ),
            success_count=0,
            failure_count=len(sample_seed.acceptance_criteria),
            total_messages=len(sample_seed.acceptance_criteria),
        )
        track_progress = AsyncMock(return_value=Result.ok(None))
        execute_parallel = AsyncMock(return_value=legacy_pause)
        parallel_executor = MagicMock()
        parallel_executor.execute_parallel = execute_parallel
        executor_cls = MagicMock(return_value=parallel_executor)
        with (
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                executor_cls,
            ),
            patch.object(runner._session_repo, "track_progress", track_progress),
            patch.object(
                runner._session_repo,
                "mark_paused",
                AsyncMock(return_value=Result.ok(True)),
            ),
            patch.object(runner, "_project_execution_outcome", AsyncMock()),
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
                force_sequential_levels=True,
                resume_execution_plan=graph.to_execution_plan(),
            )

        assert result.is_ok and result.value.success is False
        track_progress.assert_not_awaited()
        if compatibility_mode == "depth_above_durable":
            assert executor_cls.call_args.kwargs["model_router"] is runner._model_router
            assert executor_cls.call_args.kwargs["route_economics"] is runner._route_economics

    @pytest.mark.asyncio
    async def test_parallel_paused_projection_failure_preserves_owner(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Parallel auxiliary failure after PAUSED cannot escape into FAILED cleanup."""
        from ouroboros.orchestrator.heartbeat import is_holder_alive
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create(
            "exec_parallel_projection",
            sample_seed.metadata.seed_id,
            session_id="sess_parallel_projection",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
        assert generation is not None
        assert already_claimed is False
        runner._register_session(tracker.execution_id, tracker.session_id)
        message = AgentMessage(
            type="result",
            content="Quota window exhausted. Retry after 2 hours.",
            data={"subtype": "error", "error_type": "OpenCodeError"},
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=False,
                    messages=(message,),
                    final_message=message.content,
                ),
            ),
            success_count=0,
            failure_count=1,
            total_messages=1,
        )

        async def append(event: BaseEvent) -> None:
            if event.type == "execution.terminal":
                raise PersistenceError("execution projection unavailable")

        mock_event_store.append.side_effect = append

        try:
            with (
                patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
                patch(
                    "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                    AsyncMock(return_value=parallel_result),
                ),
                patch.object(
                    runner._session_repo,
                    "mark_paused",
                    AsyncMock(return_value=Result.ok(True)),
                ),
            ):
                result = await runner._execute_parallel(
                    seed=sample_seed,
                    exec_id=tracker.execution_id,
                    tracker=tracker,
                    merged_tools=["Read"],
                    tool_catalog=assemble_session_tool_catalog(["Read"]),
                    system_prompt="system",
                    start_time=tracker.start_time,
                    force_sequential_levels=True,
                )

            assert result.is_ok
            assert result.value.success is False
            assert runner._has_live_process_local_authority(
                tracker.session_id,
                tracker.execution_id,
                tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
            )
            assert is_holder_alive(tracker.session_id)
        finally:
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
            runner._unregister_session(tracker.execution_id, tracker.session_id)

    @pytest.mark.asyncio
    async def test_parallel_pause_preserves_late_cancellation_marker(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """A cancellation arriving during parallel pause publication remains pending."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create(
            "exec_parallel_pause_cancel",
            sample_seed.metadata.seed_id,
            session_id="sess_parallel_pause_cancel",
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        generation, already_claimed = runner._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            tracker.progress[EXECUTION_CONTRACT_PROGRESS_KEY],
        )
        assert generation is not None
        assert already_claimed is False
        runner._register_session(tracker.execution_id, tracker.session_id)
        message = AgentMessage(
            type="result",
            content="Quota window exhausted. Retry after 2 hours.",
            data={"subtype": "error", "error_type": "OpenCodeError"},
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=False,
                    messages=(message,),
                    final_message=message.content,
                ),
            ),
            success_count=0,
            failure_count=1,
            total_messages=1,
        )

        async def mark_paused(*args: Any, **kwargs: Any) -> Result[bool, PersistenceError]:
            del args, kwargs
            await request_cancellation(tracker.session_id)
            return Result.ok(True)

        try:
            with (
                patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
                patch(
                    "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                    AsyncMock(return_value=parallel_result),
                ),
                patch.object(runner._session_repo, "mark_paused", mark_paused),
            ):
                result = await runner._execute_parallel(
                    seed=sample_seed,
                    exec_id=tracker.execution_id,
                    tracker=tracker,
                    merged_tools=["Read"],
                    tool_catalog=assemble_session_tool_catalog(["Read"]),
                    system_prompt="system",
                    start_time=tracker.start_time,
                    force_sequential_levels=True,
                )

            assert result.is_ok
            assert await is_cancellation_requested(tracker.session_id)
        finally:
            await clear_cancellation(tracker.session_id)
            runner._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
            runner._unregister_session(tracker.execution_id, tracker.session_id)

    @pytest.mark.asyncio
    async def test_resume_session_allows_paused_sessions(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Paused sessions should remain resumable."""
        paused_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.PAUSED
        )
        paused_tracker = _attach_live_process_local_contract(
            runner,
            paused_tracker,
            sample_seed,
            session_id="sess_resume_paused",
        )
        runtime_handle = RuntimeHandle(backend="codex_cli", native_session_id="thread-123")

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Resumed successfully",
                data={"subtype": "success"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ) as mark_completed,
        ):
            result = await runner.resume_session("sess_resume_paused", sample_seed)

        assert result.is_ok
        assert result.value.success is True
        mark_completed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_public_resume_accepts_pre_anchor_managed_linked_identity(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        primary = tmp_path / "primary"
        linked = tmp_path / "linked"
        _init_git_repo(primary)
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "linked", str(linked), "HEAD"],
            cwd=primary,
            check=True,
            capture_output=True,
        )
        linked_workspace = linked / "packages" / "app"
        linked_workspace.mkdir(parents=True)
        generated = tmp_path / "managed" / "legacy-public"
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-q",
                "-b",
                "ooo/legacy-public",
                str(generated),
                "HEAD",
            ],
            cwd=primary,
            check=True,
            capture_output=True,
        )
        generated_workspace = generated / "packages" / "app"
        generated_workspace.mkdir(parents=True)
        task_workspace = TaskWorkspace(
            durable_id="legacy-public",
            repo_root=str(linked),
            repo_name="linked",
            original_cwd=str(linked_workspace),
            effective_cwd=str(generated_workspace),
            worktree_path=str(generated),
            branch="ooo/legacy-public",
            lock_path=str(tmp_path / ".locks" / "legacy-public.json"),
        )
        runner._task_workspace = task_workspace
        mock_adapter.working_directory = str(generated_workspace)
        session_id = "sess-legacy-managed-linked"
        execution_id = "exec-legacy-managed-linked"
        generation = runner._begin_process_local_authority_generation()
        contract = runner._build_execution_contract(
            project_identity=runner._project_identity(),
            seed=sample_seed,
            authority_generation=generation,
        )
        contract["frugality_proof"]["project_root"] = str(linked.resolve())
        contract["frugality_proof"]["workspace_path"] = "packages/app"
        runner._register_process_local_authority(
            session_id=session_id,
            execution_id=execution_id,
            execution_contract=contract,
            generation=generation,
        )
        runner._seal_process_local_prepared_contract(
            session_id=session_id,
            execution_id=execution_id,
            generation=generation,
            execution_contract=contract,
        )
        tracker = SessionTracker.create(
            execution_id,
            sample_seed.metadata.seed_id,
            session_id=session_id,
        ).with_status(SessionStatus.PAUSED)
        tracker = tracker.with_progress(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: contract,
                SESSION_START_IDENTITY_PROGRESS_KEY: {
                    "execution_id": execution_id,
                    "seed_id": sample_seed.metadata.seed_id,
                },
            }
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args, kwargs
            yield AgentMessage(
                type="result",
                content="Resumed managed linked session",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(tracker)),
            ),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
        ):
            result = await runner.resume_session(session_id, sample_seed)

        assert result.is_ok
        assert result.value.success is True

    @pytest.mark.asyncio
    async def test_resume_session_reuses_research_prompt_and_tool_authority(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Research resume cannot fall back to default tools or gain Edit."""
        research_seed = sample_seed.model_copy(update={"task_type": "research"})
        paused_tracker = SessionTracker.create(
            "exec_resume_research",
            research_seed.metadata.seed_id,
        ).with_status(SessionStatus.PAUSED)
        paused_tracker = _attach_live_process_local_contract(
            runner,
            paused_tracker,
            research_seed,
            session_id="sess_resume_research",
        )
        captured: dict[str, Any] = {}

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args
            captured.update(kwargs)
            yield AgentMessage(
                type="result",
                content="Research resumed successfully",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
        ):
            result = await runner.resume_session("sess_resume_research", research_seed)

        assert result.is_ok
        assert "Edit" not in captured["tools"]
        assert "research" in captured["system_prompt"].lower()

    @pytest.mark.asyncio
    async def test_resume_session_uses_frozen_context_after_manifest_changes(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """A sibling workspace edit cannot change a paused provider prompt."""
        monkeypatch.setenv("OUROBOROS_CONTEXT_PACK", "1")
        manifest = tmp_path / "pyproject.toml"
        manifest.write_text(
            '[project]\nname = "resume-app"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        mock_adapter.working_directory = str(tmp_path)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            task_cwd=str(tmp_path),
        )
        paused_tracker = SessionTracker.create(
            "exec_resume_context",
            sample_seed.metadata.seed_id,
        ).with_status(SessionStatus.PAUSED)
        paused_tracker = _attach_live_process_local_contract(
            runner,
            paused_tracker,
            sample_seed,
            session_id="sess_resume_context",
        )
        manifest.write_text(
            '[project]\nname = "resume-app"\nversion = "9.9.9"\n',
            encoding="utf-8",
        )
        captured: dict[str, Any] = {}

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            del args
            captured.update(kwargs)
            yield AgentMessage(
                type="result",
                content="Resume preserved the original prompt",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        with (
            patch.object(
                runner._session_repo,
                "reconstruct_session",
                AsyncMock(return_value=Result.ok(paused_tracker)),
            ),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
        ):
            result = await runner.resume_session("sess_resume_context", sample_seed)

        assert result.is_ok
        assert "resume-app 1.0.0" in captured["system_prompt"]
        assert "resume-app 9.9.9" not in captured["system_prompt"]

    def test_deserialize_runtime_handle_supports_legacy_progress(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test legacy Claude session progress still reconstructs a runtime handle."""
        handle = runner._deserialize_runtime_handle({"agent_session_id": "sess_legacy"})

        assert handle == RuntimeHandle(backend="claude", native_session_id="sess_legacy")

    def test_deserialize_runtime_handle_falls_back_from_invalid_runtime_payload(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Malformed runtime payloads should not block the legacy session-id fallback."""
        handle = runner._deserialize_runtime_handle(
            {
                "runtime": {
                    "native_session_id": "sess_ignored",
                    "metadata": {"server_session_id": "server-42"},
                },
                "agent_session_id": "sess_legacy",
                "runtime_backend": "claude",
            }
        )

        assert handle == RuntimeHandle(backend="claude", native_session_id="sess_legacy")

    def test_deserialize_runtime_handle_returns_none_when_invalid_payload_has_no_fallback(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Malformed runtime payloads without legacy fallback data should be ignored."""
        handle = runner._deserialize_runtime_handle(
            {
                "runtime": {
                    "native_session_id": "sess_ignored",
                    "metadata": {"server_session_id": "server-42"},
                }
            }
        )

        assert handle is None

    def test_build_progress_update_round_trips_persisted_opencode_resume_handle(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Persisted OpenCode progress should preserve the reconnect handle exactly."""
        runtime_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            updated_at="2026-03-13T00:00:00+00:00",
            metadata={
                "server_session_id": "server-42",
                "session_scope_id": "ac_1",
                "session_state_path": "execution.acceptance_criteria.ac_1.implementation_session",
                "session_role": "implementation",
                "retry_attempt": 0,
            },
        )
        message = AgentMessage(
            type="system",
            content="OpenCode session bound",
            resume_handle=runtime_handle,
        )

        progress = runner._build_progress_update(message, 2)
        restored = runner._deserialize_runtime_handle(progress)

        assert progress["runtime"] == runtime_handle.to_session_state_dict()
        assert progress["runtime_backend"] == "opencode"
        assert progress["server_session_id"] == "server-42"
        assert progress["resume_session_id"] == "server-42"
        assert restored is not None
        assert restored.backend == runtime_handle.backend
        assert restored.kind == runtime_handle.kind
        assert restored.cwd == runtime_handle.cwd
        assert restored.approval_mode == runtime_handle.approval_mode
        assert restored.metadata == runtime_handle.metadata

    @pytest.mark.asyncio
    async def test_resume_session_uses_runtime_handle(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Test resume_session passes normalized runtime handles to the adapter."""
        from ouroboros.core.types import Result

        runtime_handle = RuntimeHandle(
            backend=mock_adapter.runtime_backend,
            native_session_id="sess_runtime",
        )
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.PAUSED
        )
        running_tracker = running_tracker.with_progress({"runtime": runtime_handle.to_dict()})
        running_tracker = _attach_live_process_local_contract(
            runner,
            running_tracker,
            sample_seed,
            session_id="sess_resume_runtime_handle",
        )

        captured_kwargs: dict[str, Any] = {}

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            captured_kwargs.update(kwargs)
            yield AgentMessage(
                type="result",
                content="Resumed successfully",
                data={"subtype": "success", "session_id": "sess_runtime"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(running_tracker)

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.resume_session("sess_resume_runtime_handle", sample_seed)

        assert result.is_ok
        resume_handle = captured_kwargs["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == runtime_handle.backend
        assert resume_handle.native_session_id == runtime_handle.native_session_id
        assert resume_handle.metadata["tool_catalog"][0]["name"] == "Read"

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="OpenCode runtime not yet shipped")
    async def test_resume_session_reconnects_opencode_runtime_from_persisted_handle(
        self,
        tmp_path,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Interrupted OpenCode runs should resume from the stored runtime handle."""

        class _FakeStream:
            def __init__(self, text: str = "") -> None:
                self._buffer = text.encode("utf-8")
                self._drained = False

            async def read(self, _chunk_size: int = 16384) -> bytes:
                if self._drained:
                    return b""
                self._drained = True
                return self._buffer

        class _FakeProcess:
            def __init__(self, stdout_text: str, *, returncode: int = 0) -> None:
                self.stdout = _FakeStream(stdout_text)
                self.stderr = _FakeStream("")
                self.stdin = None
                self._returncode = returncode

            async def wait(self) -> int:
                return self._returncode

        runtime = OpenCodeRuntime(  # type: ignore[name-defined]  # noqa: F821
            cli_path="/tmp/opencode",
            permission_mode="bypassPermissions",
            cwd=tmp_path,
        )
        runner = OrchestratorRunner(runtime, mock_event_store, mock_console)

        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            cwd=str(tmp_path),
            approval_mode="acceptEdits",
            updated_at="2026-03-13T00:00:00+00:00",
            metadata={
                "server_session_id": "server-42",
                "session_scope_id": "ac_1",
                "session_state_path": ("execution.acceptance_criteria.ac_1.implementation_session"),
                "session_role": "implementation",
                "retry_attempt": 0,
            },
        )
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.RUNNING
        )
        running_tracker = running_tracker.with_progress(
            {
                "runtime": persisted_handle.to_dict(),
                "runtime_backend": "opencode",
                "messages_processed": 4,
            }
        )

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(running_tracker)

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        recorded_commands: list[tuple[str, ...]] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            recorded_commands.append(tuple(command))
            output_index = command.index("--output-last-message") + 1
            output_path = kwargs.get("cwd")
            assert output_path == str(tmp_path)
            from pathlib import Path

            Path(command[output_index]).write_text("Resume pass complete.", encoding="utf-8")
            stdout_text = (
                '{"type":"session.resumed","server_session_id":"server-42",'
                '"session":{"id":"oc-session-123"}}\n'
                '{"type":"assistant.message.delta","delta":{"text":"Reconnected to the'
                ' interrupted OpenCode session."}}\n'
            )
            return _FakeProcess(stdout_text)

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
        ):
            result = await runner.resume_session("sess_resume", sample_seed)

        assert result.is_ok
        assert result.value.success is True
        assert recorded_commands
        assert recorded_commands[0][:2] == ("/tmp/opencode", "run")
        assert "--resume" in recorded_commands[0]
        assert recorded_commands[0][recorded_commands[0].index("--resume") + 1] == "server-42"
        progress_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "orchestrator.progress.updated"
        ]
        assert any(
            event.data.get("progress", {}).get("runtime", {}).get("native_session_id")
            == "oc-session-123"
            for event in progress_events
        )

    @pytest.mark.asyncio
    async def test_resume_session_replays_persisted_progress_into_workflow_state(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Resume should rebuild workflow state from persisted progress before streaming."""
        runtime_handle = RuntimeHandle(backend="opencode", native_session_id="oc-session-123")
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.PAUSED
        )
        running_tracker = running_tracker.with_progress(
            {
                "runtime": runtime_handle.to_dict(),
                "messages_processed": 4,
            }
        )
        running_tracker = _attach_live_process_local_contract(
            runner,
            running_tracker,
            sample_seed,
            session_id="sess_resume_progress",
        )

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(running_tracker)

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute
        mock_event_store.replay.return_value = [
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess_resume_progress",
                data={
                    "message_type": "assistant",
                    "content_preview": "[AC_COMPLETE: 1] Finished the first criterion.",
                    "ac_tracking": {"started": [], "completed": [1]},
                    "progress": {
                        "last_message_type": "assistant",
                        "last_content_preview": "[AC_COMPLETE: 1] Finished the first criterion.",
                    },
                },
            )
        ]

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.resume_session("sess_resume_progress", sample_seed)

        assert result.is_ok
        workflow_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "workflow.progress.updated"
        ]
        assert workflow_events
        assert workflow_events[0].data["completed_count"] == 1
        assert workflow_events[0].data["current_ac_index"] == 2

    @pytest.mark.asyncio
    async def test_execute_parallel_passes_staged_execution_plan(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Parallel execution should pass a staged plan into the executor."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=sample_seed.acceptance_criteria[0]),
                ACNode(index=1, content=sample_seed.acceptance_criteria[1]),
                ACNode(index=2, content=sample_seed.acceptance_criteria[2], depends_on=(0, 1)),
            ),
            execution_levels=((0, 1), (2,)),
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=True,
                    final_message="done",
                ),
                ACExecutionResult(
                    ac_index=1,
                    ac_content=sample_seed.acceptance_criteria[1],
                    success=True,
                    final_message="done",
                ),
                ACExecutionResult(
                    ac_index=2,
                    ac_content=sample_seed.acceptance_criteria[2],
                    success=True,
                    final_message="done",
                ),
            ),
            success_count=3,
            failure_count=0,
            total_messages=3,
        )

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                AsyncMock(return_value=parallel_result),
            ) as mock_execute_parallel,
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok
        kwargs = mock_execute_parallel.await_args.kwargs
        execution_plan = kwargs["execution_plan"]
        assert execution_plan.execution_levels == dependency_graph.execution_levels
        assert execution_plan.total_stages == 2
        assert kwargs["session_id"] == tracker.session_id

    @pytest.mark.asyncio
    async def test_execute_parallel_passes_execution_profile_to_executor(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Runner wiring should make profile-aware decomposition live in production."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            fat_harness_mode=True,
        )
        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)
        dependency_graph = DependencyGraph(
            nodes=(ACNode(index=0, content=sample_seed.acceptance_criteria[0]),),
            execution_levels=((0,),),
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=True,
                    final_message="done",
                ),
            ),
            success_count=1,
            failure_count=0,
            total_messages=1,
        )
        captured_init: dict[str, Any] = {}

        class _FakeParallelExecutor:
            def __init__(self, **kwargs: Any) -> None:
                captured_init.update(kwargs)

            async def execute_parallel(self, **kwargs: Any) -> ParallelExecutionResult:  # noqa: ARG002
                return parallel_result

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                _FakeParallelExecutor,
            ),
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok
        profile = captured_init["execution_profile"]
        assert profile is not None
        assert profile.profile == sample_seed.task_type == "code"
        assert profile.axis == "testable_unit"
        assert captured_init["fat_harness_mode"] is True
        assert captured_init["decomposition_mode"] == "bounce_only"

    @pytest.mark.asyncio
    async def test_legacy_preflight_resume_reaches_executor_as_bounce_only(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """An old durable run migrates before the resumed executor is built."""

        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        legacy_contract = copy.deepcopy(
            runner._build_execution_contract(
                project_identity=runner._project_identity(), seed=sample_seed
            )
        )
        legacy_contract["execution_semantics"]["decomposition_mode"] = "preflight"
        legacy_contract["frugality_proof"]["execution_semantics_fingerprint"] = (
            runner._execution_semantics_fingerprint(legacy_contract["execution_semantics"])
        )
        changed, migrated_contract = runner._restore_execution_contract_snapshot(
            {EXECUTION_CONTRACT_PROGRESS_KEY: legacy_contract},
            seed=sample_seed,
        )
        assert changed is True

        tracker = SessionTracker.create("exec_preflight_resume", sample_seed.metadata.seed_id)
        dependency_graph = DependencyGraph(
            nodes=(ACNode(index=0, content=sample_seed.acceptance_criteria[0]),),
            execution_levels=((0,),),
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=True,
                    final_message="done",
                ),
            ),
            success_count=1,
            failure_count=0,
            total_messages=1,
        )
        captured_init: dict[str, Any] = {}

        class _FakeParallelExecutor:
            def __init__(self, **kwargs: Any) -> None:
                captured_init.update(kwargs)

            async def execute_parallel(self, **_kwargs: Any) -> ParallelExecutionResult:
                return parallel_result

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                _FakeParallelExecutor,
            ),
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id=tracker.execution_id,
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
                execution_contract=migrated_contract,
            )

        assert result.is_ok
        assert captured_init["decomposition_mode"] == "bounce_only"
        assert migrated_contract["execution_semantics"]["decomposition_mode"] == "bounce_only"

    @pytest.mark.asyncio
    async def test_execute_parallel_passes_fat_harness_mode_to_executor(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Runner fat-harness mode should reach the atomic executor."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            fat_harness_mode=True,
        )
        mock_adapter.aclose = AsyncMock()
        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)
        dependency_graph = DependencyGraph(
            nodes=(ACNode(index=0, content=sample_seed.acceptance_criteria[0]),),
            execution_levels=((0,),),
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=True,
                    final_message="done",
                ),
            ),
            success_count=1,
            failure_count=0,
            total_messages=1,
        )
        captured_init: dict[str, Any] = {}

        class _FakeParallelExecutor:
            def __init__(self, **kwargs: Any) -> None:
                captured_init.update(kwargs)

            async def execute_parallel(self, **kwargs: Any) -> ParallelExecutionResult:  # noqa: ARG002
                return parallel_result

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                _FakeParallelExecutor,
            ),
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok
        assert captured_init["fat_harness_mode"] is True
        mock_adapter.aclose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fat_harness_uses_profile_backed_prompt_contract(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Default fat-harness leaves must be prompted to emit typed JSON evidence."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            fat_harness_mode=True,
        )
        mock_adapter.aclose = AsyncMock()
        tracker = SessionTracker.create("exec_profile_prompt", sample_seed.metadata.seed_id)
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        expected: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        )
        captured_strategy: Any = None

        original_get_merged_tools = runner._get_merged_tools

        async def _capture_tools(**kwargs: Any) -> Any:
            nonlocal captured_strategy
            captured_strategy = kwargs["strategy"]
            return await original_get_merged_tools(**kwargs)

        with (
            patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
            patch.object(runner, "_get_merged_tools", AsyncMock(side_effect=_capture_tools)),
            patch.object(runner, "_execute_parallel", AsyncMock(return_value=expected)) as execute,
        ):
            result = await runner.execute_precreated_session(
                sample_seed,
                tracker,
                parallel=True,
            )

        assert result is expected
        assert "consolidated evidence contract" in (captured_strategy.get_system_prompt_fragment())
        system_prompt = execute.await_args.kwargs["system_prompt"]
        assert "consolidated evidence contract" in system_prompt
        assert "files_touched" in system_prompt
        assert "commands_run" in system_prompt
        assert "tests_passed" in system_prompt
        assert "evidence record as a receipt" in system_prompt
        assert "exact successful test command" in system_prompt
        mock_adapter.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fat_harness_single_ac_uses_ac_executor_path(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Single-AC fat-harness runs must not bypass the typed-evidence gate."""
        single_ac_seed = sample_seed.model_copy(
            update={"acceptance_criteria": (sample_seed.acceptance_criteria[0],)}
        )
        tracker = SessionTracker.create("exec_single", single_ac_seed.metadata.seed_id)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            fat_harness_mode=True,
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            single_ac_seed,
            session_id=tracker.session_id,
        )
        expected: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        )

        with (
            patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner,
                "_get_merged_tools",
                AsyncMock(wraps=runner._get_merged_tools),
            ),
            patch.object(runner, "_execute_parallel", AsyncMock(return_value=expected)) as execute,
        ):
            result = await runner.execute_precreated_session(
                single_ac_seed,
                tracker,
                parallel=True,
            )

        assert result is expected
        assert execute.await_args.kwargs["seed"] is single_ac_seed
        assert "force_sequential_levels" not in execute.await_args.kwargs

    @pytest.mark.asyncio
    async def test_single_ac_investment_uses_ac_executor_path(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Structured investment authority must not be lost on direct dispatch."""
        investment_seed = sample_seed.model_copy(
            update={
                "acceptance_criteria": (
                    AcceptanceCriterionSpec(
                        description="Rotate production signing keys",
                        investment=InvestmentSpec(
                            difficulty="medium",
                            stakes="high",
                            provenance="declared",
                            confidence="high",
                        ),
                    ),
                )
            }
        )
        tracker = SessionTracker.create("exec_investment_single", investment_seed.metadata.seed_id)
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            investment_seed,
            session_id=tracker.session_id,
        )
        expected: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        )

        with (
            patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner,
                "_get_merged_tools",
                AsyncMock(wraps=runner._get_merged_tools),
            ),
            patch.object(runner, "_execute_parallel", AsyncMock(return_value=expected)) as execute,
        ):
            result = await runner.execute_precreated_session(
                investment_seed,
                tracker,
                parallel=True,
            )

        assert result is expected
        assert execute.await_args.kwargs["seed"] is investment_seed
        assert "force_sequential_levels" not in execute.await_args.kwargs

    @pytest.mark.asyncio
    async def test_incomplete_investment_metadata_uses_ac_executor_path(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Any explicit metadata uses per-AC routing, even without lowering authority."""
        investment_seed = sample_seed.model_copy(
            update={
                "acceptance_criteria": (
                    AcceptanceCriterionSpec(
                        description="Inspect the deployment boundary",
                        investment=InvestmentSpec(stakes="medium"),
                    ),
                )
            }
        )
        tracker = SessionTracker.create(
            "exec_investment_incomplete",
            investment_seed.metadata.seed_id,
        )
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            investment_seed,
            session_id=tracker.session_id,
        )
        expected: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        )

        with (
            patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner,
                "_get_merged_tools",
                AsyncMock(wraps=runner._get_merged_tools),
            ),
            patch.object(runner, "_execute_parallel", AsyncMock(return_value=expected)) as execute,
        ):
            result = await runner.execute_precreated_session(
                investment_seed,
                tracker,
                parallel=True,
            )

        assert result is expected
        assert execute.await_args.kwargs["seed"] is investment_seed

    @pytest.mark.asyncio
    async def test_sequential_investment_seed_preserves_one_ac_per_stage(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Investment-bearing legacy sequential runs retain sequential semantics."""
        investment_seed = sample_seed.model_copy(
            update={
                "acceptance_criteria": (
                    sample_seed.acceptance_criteria[0],
                    AcceptanceCriterionSpec(
                        description="Migrate the payment ledger",
                        investment=InvestmentSpec(
                            difficulty="high",
                            stakes="high",
                            provenance="measured",
                            confidence="high",
                        ),
                    ),
                )
            }
        )
        tracker = SessionTracker.create(
            "exec_investment_sequential",
            investment_seed.metadata.seed_id,
        )
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            investment_seed,
            session_id=tracker.session_id,
        )
        expected: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        )

        with (
            patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner,
                "_get_merged_tools",
                AsyncMock(wraps=runner._get_merged_tools),
            ),
            patch.object(runner, "_execute_parallel", AsyncMock(return_value=expected)) as execute,
        ):
            result = await runner.execute_precreated_session(
                investment_seed,
                tracker,
                parallel=False,
            )

        assert result is expected
        assert execute.await_args.kwargs["seed"] is investment_seed
        assert execute.await_args.kwargs["force_sequential_levels"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_sequential_run_uses_sequential_ac_executor_plan(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """--sequential plus fat-harness should preserve ordering and enforce AC gates."""
        tracker = SessionTracker.create("exec_sequential", sample_seed.metadata.seed_id)
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            fat_harness_mode=True,
        )
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        expected: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        )

        with (
            patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner,
                "_get_merged_tools",
                AsyncMock(wraps=runner._get_merged_tools),
            ),
            patch.object(runner, "_execute_parallel", AsyncMock(return_value=expected)) as execute,
        ):
            result = await runner.execute_precreated_session(sample_seed, tracker, parallel=False)

        assert result is expected
        assert execute.await_args.kwargs["force_sequential_levels"] is True

    @pytest.mark.asyncio
    async def test_force_sequential_levels_direct_caller_uses_ac_executor_path(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Direct runner callers can request one-AC-per-stage executor routing."""
        tracker = SessionTracker.create("exec_forced", sample_seed.metadata.seed_id)
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        tracker = _attach_live_process_local_contract(
            runner,
            tracker,
            sample_seed,
            session_id=tracker.session_id,
        )
        expected: Result[OrchestratorResult, OrchestratorError] = Result.ok(
            OrchestratorResult(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        )

        with (
            patch.object(runner, "_check_startup_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner,
                "_get_merged_tools",
                AsyncMock(wraps=runner._get_merged_tools),
            ),
            patch.object(runner, "_execute_parallel", AsyncMock(return_value=expected)) as execute,
        ):
            result = await runner.execute_precreated_session(
                sample_seed,
                tracker,
                force_sequential_levels=True,
            )

        assert result is expected
        assert execute.await_args.kwargs["force_sequential_levels"] is True

    @pytest.mark.asyncio
    async def test_force_sequential_levels_preserves_one_ac_per_stage(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """The AC executor can honor --sequential without dependency analysis."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=True,
                    final_message="done",
                ),
            ),
            success_count=1,
            failure_count=0,
            total_messages=1,
        )

        with (
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                AsyncMock(return_value=parallel_result),
            ) as execute,
            patch.object(runner, "_build_dependency_analyzer") as analyzer_factory,
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
                force_sequential_levels=True,
            )

        assert result.is_ok
        analyzer_factory.assert_not_called()
        execution_plan = execute.await_args.kwargs["execution_plan"]
        assert execution_plan.execution_levels == ((0,), (1,), (2,))
        assert execution_plan.get_dependencies(0) == ()
        assert execution_plan.get_dependencies(1) == (0,)
        assert execution_plan.get_dependencies(2) == (0, 1)

    @pytest.mark.asyncio
    async def test_execute_parallel_builds_dependency_analyzer_with_llm_adapter(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Parallel execution should restore LLM-assisted dependency analysis."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        runner._adapter.llm_backend = None
        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)
        dependency_graph = DependencyGraph(
            nodes=tuple(
                ACNode(index=i, content=ac) for i, ac in enumerate(sample_seed.acceptance_criteria)
            ),
            execution_levels=((0, 1, 2),),
        )
        parallel_result = ParallelExecutionResult(
            results=tuple(
                ACExecutionResult(
                    ac_index=i,
                    ac_content=ac,
                    success=True,
                    final_message="done",
                )
                for i, ac in enumerate(sample_seed.acceptance_criteria)
            ),
            success_count=len(sample_seed.acceptance_criteria),
            failure_count=0,
            total_messages=len(sample_seed.acceptance_criteria),
        )
        llm_adapter = object()
        analyzer_instance = MagicMock()
        analyzer_instance.analyze = AsyncMock(return_value=Result.ok(dependency_graph))
        dependency_analyzer_cls = MagicMock(return_value=analyzer_instance)

        with (
            patch(
                "ouroboros.orchestrator.runner.create_llm_adapter",
                return_value=llm_adapter,
            ) as mock_create_llm_adapter,
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer",
                dependency_analyzer_cls,
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                AsyncMock(return_value=parallel_result),
            ),
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok
        mock_create_llm_adapter.assert_called_once_with(
            backend="opencode",
            permission_mode="bypassPermissions",
            cli_path=None,
            cwd=_EXPECTED_CANONICAL_PROJECT_CWD,
            max_turns=1,
            allowed_tools=[],
        )
        dependency_analyzer_cls.assert_called_once_with(llm_adapter=llm_adapter, model="default")

    def test_build_dependency_analyzer_reuses_resolved_codex_cli_path(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Nested dependency analysis should inherit the runtime's resolved Codex CLI path."""
        mock_adapter.runtime_backend = "codex"
        mock_adapter.llm_backend = None
        mock_adapter.cli_path = "/tmp/real-codex"
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        llm_adapter = object()
        dependency_analyzer_cls = MagicMock()

        with (
            patch(
                "ouroboros.orchestrator.runner.create_llm_adapter",
                return_value=llm_adapter,
            ) as mock_create_llm_adapter,
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer",
                dependency_analyzer_cls,
            ),
        ):
            analyzer = runner._build_dependency_analyzer()

        assert analyzer is dependency_analyzer_cls.return_value
        mock_create_llm_adapter.assert_called_once_with(
            backend="codex",
            permission_mode="bypassPermissions",
            cli_path="/tmp/real-codex",
            cwd=_EXPECTED_CANONICAL_PROJECT_CWD,
            max_turns=1,
            allowed_tools=[],
        )

    def test_build_dependency_analyzer_drops_runtime_cli_path_for_different_llm_backend(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        mock_adapter.runtime_backend = "codex"
        mock_adapter.llm_backend = "claude_code"
        mock_adapter.cli_path = "/tmp/real-codex"
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        with patch("ouroboros.orchestrator.runner.create_llm_adapter") as mock_create_llm_adapter:
            runner._build_dependency_analyzer()

        assert mock_create_llm_adapter.call_args.kwargs["backend"] == "claude_code"
        assert mock_create_llm_adapter.call_args.kwargs["cli_path"] is None

    def test_build_dependency_analyzer_drops_runtime_cli_path_for_runtime_only_backend(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        mock_adapter.runtime_backend = "grok"
        mock_adapter.llm_backend = "claude_code"
        mock_adapter.cli_path = "/tmp/real-grok"
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        with patch("ouroboros.orchestrator.runner.create_llm_adapter") as mock_create_llm_adapter:
            runner._build_dependency_analyzer()

        assert mock_create_llm_adapter.call_args.kwargs["backend"] == "claude_code"
        assert mock_create_llm_adapter.call_args.kwargs["cli_path"] is None

    @pytest.mark.parametrize(
        ("runtime_backend", "llm_backend", "cli_path"),
        [
            ("codex_mcp", "codex", "/tmp/real-codex"),
            ("claude_mcp", "claude_code", "/tmp/real-claude"),
        ],
    )
    def test_build_dependency_analyzer_reuses_cli_path_for_same_cli_worker_transport(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        runtime_backend: str,
        llm_backend: str,
        cli_path: str,
    ) -> None:
        mock_adapter.runtime_backend = runtime_backend
        mock_adapter.llm_backend = llm_backend
        mock_adapter.cli_path = cli_path
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        with patch("ouroboros.orchestrator.runner.create_llm_adapter") as mock_create_llm_adapter:
            runner._build_dependency_analyzer()

        assert mock_create_llm_adapter.call_args.kwargs["cli_path"] == cli_path

    def test_build_dependency_analyzer_uses_public_llm_backend_property(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """_build_dependency_analyzer must use the public llm_backend property, not _llm_backend."""
        mock_adapter.runtime_backend = "opencode"
        mock_adapter.llm_backend = "codex"  # override via public property
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        llm_adapter = object()
        dependency_analyzer_cls = MagicMock()

        with (
            patch(
                "ouroboros.orchestrator.runner.create_llm_adapter",
                return_value=llm_adapter,
            ) as mock_create_llm_adapter,
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer",
                dependency_analyzer_cls,
            ),
        ):
            analyzer = runner._build_dependency_analyzer()

        assert analyzer is dependency_analyzer_cls.return_value
        # llm_backend="codex" takes precedence over runtime_backend="opencode"
        mock_create_llm_adapter.assert_called_once_with(
            backend="codex",
            permission_mode="bypassPermissions",
            cli_path=None,
            cwd=_EXPECTED_CANONICAL_PROJECT_CWD,
            max_turns=1,
            allowed_tools=[],
        )

    def test_build_dependency_analyzer_falls_back_to_runtime_backend_when_llm_backend_none(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """When llm_backend is None, runtime_backend is used as the LLM backend."""
        mock_adapter.runtime_backend = "opencode"
        mock_adapter.llm_backend = None
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        llm_adapter = object()
        dependency_analyzer_cls = MagicMock()

        with (
            patch(
                "ouroboros.orchestrator.runner.create_llm_adapter",
                return_value=llm_adapter,
            ) as mock_create_llm_adapter,
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer",
                dependency_analyzer_cls,
            ),
        ):
            analyzer = runner._build_dependency_analyzer()

        assert analyzer is dependency_analyzer_cls.return_value
        mock_create_llm_adapter.assert_called_once_with(
            backend="opencode",
            permission_mode="bypassPermissions",
            cli_path=None,
            cwd=_EXPECTED_CANONICAL_PROJECT_CWD,
            max_turns=1,
            allowed_tools=[],
        )

    def test_plan_parallel_workers_serializes_cli_backend(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """A CLI backend (no known LLM limits) is serialized regardless of the
        configured worker count (R3 stampede guard)."""
        mock_adapter.runtime_backend = "hermes_cli"
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            max_parallel_workers=3,
        )

        assert runner._plan_parallel_workers() == 1

    def test_plan_parallel_workers_respects_native_claude(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """The native Claude backend is governed by its rate bucket, so fan-out
        is not concurrency-capped here."""
        mock_adapter.runtime_backend = "claude"
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            max_parallel_workers=3,
        )

        assert runner._plan_parallel_workers() == 3

    def test_plan_parallel_workers_honors_env_override(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Operators can raise the cap for a CLI backend via env override."""
        monkeypatch.setenv("OUROBOROS_MAX_CONCURRENCY", "2")
        mock_adapter.runtime_backend = "hermes_cli"
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            max_parallel_workers=5,
        )

        assert runner._plan_parallel_workers() == 2

    def test_build_dependency_analyzer_catches_expected_exceptions(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """RuntimeError from create_llm_adapter is caught and returns a no-LLM analyzer."""
        from ouroboros.orchestrator.dependency_analyzer import DependencyAnalyzer

        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        with patch(
            "ouroboros.orchestrator.runner.create_llm_adapter",
            side_effect=RuntimeError("CLI not found"),
        ):
            analyzer = runner._build_dependency_analyzer()

        assert isinstance(analyzer, DependencyAnalyzer)

    def test_build_dependency_analyzer_falls_back_for_invalid_llm_backend(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        from ouroboros.orchestrator.dependency_analyzer import DependencyAnalyzer

        mock_adapter.runtime_backend = "codex"
        mock_adapter.llm_backend = "not-a-backend"
        mock_adapter.cli_path = "/tmp/real-codex"
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        analyzer = runner._build_dependency_analyzer()

        assert isinstance(analyzer, DependencyAnalyzer)

    def test_build_dependency_analyzer_does_not_catch_type_error(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """TypeError from create_llm_adapter propagates uncaught (programming error)."""
        mock_adapter.llm_backend = None
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        with (
            patch(
                "ouroboros.orchestrator.runner.create_llm_adapter",
                side_effect=TypeError("unexpected keyword argument"),
            ),
            pytest.raises(TypeError),
        ):
            runner._build_dependency_analyzer()

    def test_build_dependency_analyzer_does_not_catch_attribute_error(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """AttributeError from create_llm_adapter propagates uncaught (programming error)."""
        mock_adapter.llm_backend = None
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

        with (
            patch(
                "ouroboros.orchestrator.runner.create_llm_adapter",
                side_effect=AttributeError("object has no attribute"),
            ),
            pytest.raises(AttributeError),
        ):
            runner._build_dependency_analyzer()

    def test_legacy_adapter_without_llm_backend_degrades_gracefully(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Legacy adapters predating v0.28.6 (no llm_backend attr) fall back to structured-only.

        Protects downstream Protocol implementers (custom runtimes, test mocks)
        from the v0.28.6 AgentRuntime Protocol addition. Instead of raising
        AttributeError at the call site, _build_dependency_analyzer returns a
        structured-only DependencyAnalyzer, preserving pre-v0.28.6 behavior.
        """
        from ouroboros.orchestrator.dependency_analyzer import DependencyAnalyzer

        class LegacyAdapter:
            """Pre-v0.28.6 adapter stub without llm_backend attribute."""

            runtime_backend = "opencode"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

        legacy_adapter = LegacyAdapter()
        assert not hasattr(legacy_adapter, "llm_backend")

        runner = OrchestratorRunner(legacy_adapter, mock_event_store, mock_console)  # type: ignore[arg-type]

        with patch("ouroboros.orchestrator.runner.create_llm_adapter") as mock_create_llm_adapter:
            analyzer = runner._build_dependency_analyzer()

        # Must not attempt to wire an LLM adapter when the legacy runtime
        # lacks llm_backend - that path is the breaking-change path.
        mock_create_llm_adapter.assert_not_called()
        assert isinstance(analyzer, DependencyAnalyzer)

    @pytest.mark.asyncio
    async def test_execute_seed_uses_inherited_runtime_handle(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
        tmp_path: Path,
    ) -> None:
        """Delegated child runs should fork from the inherited parent runtime handle."""
        inherited_handle = RuntimeHandle(
            backend="claude",
            native_session_id="sess_parent",
            metadata={"fork_session": True},
        )
        mock_adapter = MagicMock()
        mock_adapter.runtime_backend = "claude"
        mock_adapter.llm_backend = "test-llm"
        mock_adapter._model = "test-model"
        project = tmp_path / "project"
        project.mkdir()
        mock_adapter.working_directory = str(project)
        mock_adapter.permission_mode = "acceptEdits"
        captured_kwargs: dict[str, Any] = {}

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            captured_kwargs.update(kwargs)
            yield AgentMessage(
                type="result",
                content="Task completed successfully",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        # Inherit a known builtin (WebFetch) and a bridge tool
        # (mcp__chrome-devtools__click).  The bridge tool should be
        # deferred to MCPToolProvider discovery — not injected as a
        # phantom builtin catalog entry.
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            inherited_runtime_handle=inherited_handle,
            inherited_tools=["WebFetch", "mcp__chrome-devtools__click"],
            fat_harness_mode=False,
        )
        _allow_mocked_precreated_durable_state(runner)

        from ouroboros.core.types import Result

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        resume_handle = captured_kwargs["resume_handle"]
        assert resume_handle is not None
        assert resume_handle.backend == inherited_handle.backend
        assert resume_handle.native_session_id == inherited_handle.native_session_id
        assert resume_handle.metadata.get("fork_session") is True
        # Known builtin should be inherited
        assert "WebFetch" in captured_kwargs["tools"]
        # Bridge tool should NOT appear — it would be a phantom entry
        assert "mcp__chrome-devtools__click" not in captured_kwargs["tools"]

    @pytest.mark.asyncio
    async def test_execute_parallel_restores_profile_and_inherited_handle_from_contract(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Parallel replay must not adopt current profile or parent lineage drift."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
        from ouroboros.orchestrator.parallel_executor import (
            ACExecutionResult,
            ParallelExecutionResult,
        )

        inherited_handle = RuntimeHandle(
            backend="claude",
            native_session_id="sess_parent",
            metadata={"fork_session": True},
        )
        mock_adapter = MagicMock()
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            inherited_runtime_handle=inherited_handle,
        )
        execution_contract = runner._build_execution_contract(
            project_identity=runner._project_identity(), seed=sample_seed
        )
        persisted_profile = runner._execution_profile_snapshot(
            execution_contract,
            require_bound=True,
        )
        assert persisted_profile is not None
        drifted_profile = persisted_profile.model_copy(
            update={"suggested_model_tier": SuggestedModelTier.HIGH}
        )
        runner._inherited_runtime_handle = RuntimeHandle(
            backend="claude",
            native_session_id="sess_drifted_parent",
            metadata={"fork_session": True},
        )
        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)
        captured_init: dict[str, Any] = {}
        captured_execute: dict[str, Any] = {}

        class FakeParallelExecutor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured_init.update(kwargs)

            async def execute_parallel(self, **kwargs: Any) -> ParallelExecutionResult:
                captured_execute.update(kwargs)
                return ParallelExecutionResult(
                    results=tuple(
                        ACExecutionResult(
                            ac_index=index,
                            ac_content=ac,
                            success=True,
                            final_message="[TASK_COMPLETE]",
                        )
                        for index, ac in enumerate(sample_seed.acceptance_criteria)
                    ),
                    success_count=len(sample_seed.acceptance_criteria),
                    failure_count=0,
                    total_messages=3,
                    total_duration_seconds=0.1,
                )

        dependency_graph = DependencyGraph(
            nodes=tuple(
                ACNode(index=index, content=ac)
                for index, ac in enumerate(sample_seed.acceptance_criteria)
            ),
            execution_levels=(tuple(range(len(sample_seed.acceptance_criteria))),),
        )

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                FakeParallelExecutor,
            ),
            patch(
                "ouroboros.orchestrator.runner._execution_profile_for_seed",
                return_value=drifted_profile,
            ) as load_current_profile,
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo, "mark_completed", AsyncMock(return_value=Result.ok(None))
            ),
        ):
            from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

            tool_catalog = assemble_session_tool_catalog(
                ["Read", "mcp__chrome-devtools__click"],
            )
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read", "mcp__chrome-devtools__click"],
                tool_catalog=tool_catalog,
                system_prompt="system",
                start_time=datetime.now(UTC),
                execution_contract=execution_contract,
            )

        assert result.is_ok
        inherited_runtime_handle = captured_init["inherited_runtime_handle"]
        assert inherited_runtime_handle.native_session_id == inherited_handle.native_session_id
        assert inherited_runtime_handle.metadata == inherited_handle.metadata
        assert inherited_runtime_handle.approval_mode == "bypassPermissions"
        assert captured_init["execution_profile"] == persisted_profile
        assert captured_init["execution_profile"].suggested_model_tier is SuggestedModelTier.MEDIUM
        load_current_profile.assert_not_called()
        assert captured_execute["tools"] == ["Read", "mcp__chrome-devtools__click"]

    @pytest.mark.asyncio
    async def test_execute_parallel_emits_verification_report_for_decomposed_acs(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Parallel execution should preserve decomposed Sub-AC evidence for QA."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
        from ouroboros.orchestrator.parallel_executor import (
            ACExecutionResult,
            ParallelExecutionResult,
        )

        runner = OrchestratorRunner(MagicMock(), mock_event_store, mock_console)
        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)

        sub_result = ACExecutionResult(
            ac_index=100,
            ac_content="Create task storage",
            success=True,
            messages=(
                AgentMessage(
                    type="tool",
                    content="Running tests",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": "uv   run pytest\n tests/unit/test_runner.py -q"}
                    },
                ),
                AgentMessage(
                    type="tool",
                    content="Writing file",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "/tmp/project/task_store.py"}},
                ),
            ),
            final_message="Implemented task storage and verified behavior.",
        )

        class FakeParallelExecutor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def execute_parallel(self, **kwargs: Any) -> ParallelExecutionResult:
                return ParallelExecutionResult(
                    results=(
                        ACExecutionResult(
                            ac_index=0,
                            ac_content=sample_seed.acceptance_criteria[0],
                            success=True,
                            is_decomposed=True,
                            sub_results=(sub_result,),
                            final_message="Decomposed placeholder should not leak",
                        ),
                        ACExecutionResult(
                            ac_index=1,
                            ac_content=sample_seed.acceptance_criteria[1],
                            success=True,
                            final_message="Listed tasks correctly.",
                        ),
                        ACExecutionResult(
                            ac_index=2,
                            ac_content=sample_seed.acceptance_criteria[2],
                            success=True,
                            final_message="Deleted tasks correctly.",
                        ),
                    ),
                    success_count=3,
                    failure_count=0,
                    total_messages=4,
                    total_duration_seconds=0.2,
                )

        dependency_graph = DependencyGraph(
            nodes=tuple(
                ACNode(index=index, content=ac)
                for index, ac in enumerate(sample_seed.acceptance_criteria)
            ),
            execution_levels=(tuple(range(len(sample_seed.acceptance_criteria))),),
        )

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                FakeParallelExecutor,
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo, "mark_completed", AsyncMock(return_value=Result.ok(None))
            ),
        ):
            from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read", "Write", "Bash"],
                tool_catalog=assemble_session_tool_catalog(["Read", "Write", "Bash"]),
                system_prompt="system",
                start_time=datetime.now(UTC),
            )

        assert result.is_ok
        assert "Commands Run:" not in result.value.final_message
        assert "Task Status:" in result.value.final_message
        verification_report = result.value.summary["verification_report"]
        assert "### Task 1: [COMPLETED] Tasks can be created" in verification_report
        assert "#### Subtask 1.1: [COMPLETED] Create task storage" in verification_report
        assert "Bash: uv run pytest tests/unit/test_runner.py -q" in verification_report
        assert "Write: /tmp/project/task_store.py" in verification_report
        assert "Decomposed placeholder should not leak" not in verification_report


class TestOrchestratorError:
    """Tests for OrchestratorError."""

    def test_create_error(self) -> None:
        """Test creating an orchestrator error."""
        error = OrchestratorError(
            message="Execution failed",
            details={"session_id": "sess_123"},
        )
        assert "Execution failed" in str(error)

    def test_error_with_details(self) -> None:
        """Test error includes details."""
        error = OrchestratorError(
            message="Failed",
            details={"code": 500, "reason": "timeout"},
        )
        assert error.details is not None
        assert error.details["code"] == 500


class TestOrchestratorRunnerWithMCP:
    """Tests for OrchestratorRunner with MCP integration."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create a mock Claude agent adapter."""
        Path("/tmp/project").mkdir(parents=True, exist_ok=True)
        adapter = MagicMock()
        adapter.runtime_backend = "opencode"
        adapter.llm_backend = "test-llm"
        adapter._model = "test-model"
        adapter.working_directory = "/tmp/project"
        adapter.permission_mode = "acceptEdits"
        return adapter

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        store.query_execution_related_events = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock Rich console."""
        return MagicMock()

    @pytest.fixture
    def mock_mcp_manager(self) -> MagicMock:
        """Create a mock MCP client manager."""
        from ouroboros.mcp.types import MCPToolDefinition

        manager = MagicMock()
        manager.list_all_tools = AsyncMock(
            return_value=[
                MCPToolDefinition(
                    name="external_tool",
                    description="An external MCP tool",
                    server_name="test-server",
                ),
            ]
        )
        return manager

    def test_init_with_mcp_manager(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test runner initialization with MCP manager."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        assert runner.mcp_manager is mock_mcp_manager

    def test_init_without_mcp_manager(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Test runner initialization without MCP manager."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
        )

        assert runner.mcp_manager is None

    def test_init_with_mcp_tool_prefix(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test runner initialization with MCP tool prefix."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
            mcp_tool_prefix="ext_",
        )

        assert runner._mcp_tool_prefix == "ext_"

    @pytest.mark.asyncio
    async def test_get_merged_tools_without_mcp(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Test getting merged tools without MCP manager."""
        from ouroboros.orchestrator.adapter import DEFAULT_TOOLS

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
        )

        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        assert merged_tools == DEFAULT_TOOLS
        assert provider is None
        assert [tool.name for tool in tool_catalog.tools] == DEFAULT_TOOLS

    @pytest.mark.asyncio
    async def test_get_merged_tools_with_mcp(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test getting merged tools with MCP manager."""
        from ouroboros.orchestrator.adapter import DEFAULT_TOOLS

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        # Should include DEFAULT_TOOLS + MCP tools
        assert all(t in merged_tools for t in DEFAULT_TOOLS)
        assert "external_tool" in merged_tools
        assert provider is not None
        assert tool_catalog.attached_tools[0].name == "external_tool"

    @pytest.mark.asyncio
    async def test_get_merged_tools_uses_deterministic_session_catalog_order(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Merged tool order should come from the normalized session catalog."""
        from ouroboros.mcp.types import MCPToolDefinition

        class _Strategy:
            def get_tools(self) -> list[str]:
                return ["Write", "Read"]

        mock_mcp_manager.list_all_tools = AsyncMock(
            return_value=[
                MCPToolDefinition(
                    name="search",
                    description="Search from server-b",
                    server_name="server-b",
                ),
                MCPToolDefinition(
                    name="Read",
                    description="Conflicting read tool",
                    server_name="server-shadow",
                ),
                MCPToolDefinition(
                    name="alpha",
                    description="Alpha tool",
                    server_name="server-a",
                ),
                MCPToolDefinition(
                    name="search",
                    description="Search from server-a",
                    server_name="server-a",
                ),
            ]
        )

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        merged_tools, provider, tool_catalog = await runner._get_merged_tools(
            "session_123",
            strategy=_Strategy(),
        )

        assert merged_tools == ["Write", "Read", "alpha", "search"]
        assert provider is not None
        assert [tool.name for tool in provider.session_catalog.tools] == merged_tools
        assert [tool.name for tool in tool_catalog.tools] == merged_tools

    @pytest.mark.asyncio
    async def test_get_merged_tools_includes_inherited_tools(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Inherited builtin tools are merged; non-builtin tools are preserved as capabilities."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            inherited_tools=["Read", "WebFetch", "mcp__chrome-devtools__click"],
        )

        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        # Known builtins are inherited and deduplicated
        assert "WebFetch" in merged_tools
        assert merged_tools.count("Read") == 1
        # Bridge/MCP tools are NOT in merged_tools — they would create
        # phantom entries.  Instead they are preserved as inherited
        # capabilities on the catalog for authorization / observability.
        assert "mcp__chrome-devtools__click" not in merged_tools
        assert tool_catalog is not None
        assert "mcp__chrome-devtools__click" in tool_catalog.inherited_capabilities
        assert provider is None

    @pytest.mark.asyncio
    async def test_get_merged_tools_emits_policy_events_for_inherited_capabilities(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Policy decisions are persisted, including non-executable inherited grants."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            inherited_tools=["Read", "mcp__chrome-devtools__click"],
        )

        merged_tools, provider, _tool_catalog = await runner._get_merged_tools("session_123")

        assert provider is None
        assert "mcp__chrome-devtools__click" not in merged_tools
        policy_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "orchestrator.policy.capabilities.evaluated"
        ]
        assert len(policy_events) == 1
        events_by_name = {
            item["capability"]["name"]: item for item in policy_events[0].data["evaluations"]
        }

        read_event = events_by_name["Read"]
        assert read_event["decision"]["visible"] is True
        assert read_event["decision"]["executable"] is True

        inherited_event = events_by_name["mcp__chrome-devtools__click"]
        assert inherited_event["capability"]["source_kind"] == "inherited_capability"
        assert inherited_event["decision"]["visible"] is True
        assert inherited_event["decision"]["executable"] is False
        assert inherited_event["decision"]["reasons"] == [
            "inherited_capability requires live provider discovery before execution"
        ]

    @pytest.mark.asyncio
    async def test_policy_audit_emit_failure_does_not_break_orchestration(
        self,
        mock_adapter: MagicMock,
        mock_console: MagicMock,
    ) -> None:
        """A failing event store must not take down tool-catalog assembly.

        The policy audit event is auxiliary observability, not a
        prerequisite for orchestration.  If the event store fails
        (disk full, DB locked, etc.), the orchestrator must keep
        producing merged tools and degrade to a logged warning
        instead of raising into the session flow.
        """
        failing_event_store = AsyncMock()
        failing_event_store.append.side_effect = RuntimeError("event store unavailable")

        runner = OrchestratorRunner(
            mock_adapter,
            failing_event_store,
            mock_console,
            inherited_tools=["Read"],
        )

        # Must not raise despite every append() failing.
        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        assert provider is None
        assert "Read" in merged_tools
        assert tool_catalog is not None
        # The failed audit attempt was still made (best-effort, not skipped).
        assert failing_event_store.append.await_count >= 1

    @pytest.mark.asyncio
    async def test_get_merged_tools_preserves_inherited_capabilities_after_discovery(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Inherited MCP capabilities survive MCP discovery replacing session_catalog."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
            inherited_tools=["Read", "mcp__chrome-devtools__click"],
        )

        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        # MCP discovery succeeded — provider found 'external_tool'
        assert provider is not None
        assert "external_tool" in merged_tools
        # The inherited MCP capability must survive discovery replacing
        # the catalog — this was Regression 2 from Q00's review.
        assert tool_catalog is not None
        assert "mcp__chrome-devtools__click" in tool_catalog.inherited_capabilities

    @pytest.mark.asyncio
    async def test_runtime_handle_tool_catalog_round_trip_with_inherited_capabilities(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Serialized dict tool catalog round-trips through runtime_handle_tool_catalog."""
        from dataclasses import replace as dc_replace

        from ouroboros.orchestrator.adapter import RuntimeHandle, runtime_handle_tool_catalog
        from ouroboros.orchestrator.mcp_tools import (
            assemble_session_tool_catalog,
            normalize_serialized_tool_catalog,
            serialize_tool_catalog,
        )

        catalog = assemble_session_tool_catalog(["Read", "Edit"])
        catalog = dc_replace(
            catalog,
            inherited_capabilities=frozenset({"mcp__chrome-devtools__click"}),
        )
        serialized = serialize_tool_catalog(catalog)
        # serialize_tool_catalog returns dict when inherited_capabilities present
        assert isinstance(serialized, dict)

        handle = RuntimeHandle(
            backend="claude",
            native_session_id="s1",
            metadata={"tool_catalog": serialized},
        )
        # runtime_handle_tool_catalog must accept the dict format
        raw = runtime_handle_tool_catalog(handle)
        assert raw is not None
        assert isinstance(raw, dict)

        # Full round-trip through normalize_serialized_tool_catalog
        restored = normalize_serialized_tool_catalog(raw)
        assert restored is not None
        assert "mcp__chrome-devtools__click" in restored.inherited_capabilities

    @pytest.mark.asyncio
    async def test_get_merged_tools_mcp_failure(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test graceful handling when MCP tool listing fails."""
        from ouroboros.orchestrator.adapter import DEFAULT_TOOLS

        mock_mcp_manager.list_all_tools = AsyncMock(side_effect=Exception("Connection lost"))

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        # Should still return DEFAULT_TOOLS on failure
        assert merged_tools == DEFAULT_TOOLS
        # Provider is still returned (error is handled gracefully inside MCPToolProvider)
        # This allows callers to retry or check provider state
        assert provider is not None
        # No MCP tools should have been added
        assert len(merged_tools) == len(DEFAULT_TOOLS)
        assert tool_catalog.attached_tools == ()

    @pytest.mark.asyncio
    async def test_execute_seed_with_mcp_tools(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Test seed execution uses merged tools."""
        from ouroboros.core.types import Result

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="result",
                content="Done",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )
        _allow_mocked_precreated_durable_state(runner)

        # Mock session creation
        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_completed", mock_mark_completed):
                result = await runner.execute_seed(sample_seed)

        assert result.is_ok
        # MCP tools loaded event should have been emitted
        assert mock_event_store.append.called


class TestCancellationPolling:
    """Tests for cancellation detection in execution loops."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create a mock Claude agent adapter."""
        Path("/tmp/project").mkdir(parents=True, exist_ok=True)
        adapter = MagicMock()
        adapter.runtime_backend = "opencode"
        adapter.llm_backend = "test-llm"
        adapter._model = "test-model"
        adapter.working_directory = "/tmp/project"
        adapter.permission_mode = "acceptEdits"
        return adapter

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        store.query_events = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock Rich console."""
        return MagicMock()

    @pytest.fixture
    def runner(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> OrchestratorRunner:
        """Create a runner with mocked dependencies."""
        runner = OrchestratorRunner(mock_adapter, mock_event_store, mock_console)
        _allow_mocked_precreated_durable_state(runner)
        return runner

    @pytest.mark.asyncio
    async def test_check_cancellation_returns_false_when_no_event(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test _check_cancellation returns False when no cancellation event exists."""
        mock_event_store.query_events = AsyncMock(return_value=[])
        result = await runner._check_cancellation("session_123")
        assert result is False
        mock_event_store.query_events.assert_called_once_with(
            aggregate_id="session_123",
            event_type="orchestrator.session.cancelled",
            limit=1,
        )

    @pytest.mark.asyncio
    async def test_check_cancellation_returns_true_when_event_exists(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test _check_cancellation returns True when cancellation event exists."""
        from ouroboros.orchestrator.events import create_session_cancelled_event

        cancel_event = create_session_cancelled_event("session_123", "User requested")
        mock_event_store.query_events = AsyncMock(return_value=[cancel_event])
        result = await runner._check_cancellation("session_123")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_cancellation_graceful_on_error(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test _check_cancellation returns False on event store error (graceful degradation)."""
        mock_event_store.query_events = AsyncMock(side_effect=Exception("DB unavailable"))
        result = await runner._check_cancellation("session_123")
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_cancellation_returns_result(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test _handle_cancellation returns a proper OrchestratorResult."""
        from datetime import UTC, datetime

        start_time = datetime.now(UTC)

        with patch.object(runner._session_repo, "mark_cancelled", AsyncMock(return_value=None)):
            result = await runner._handle_cancellation(
                session_id="sess_123",
                execution_id="exec_456",
                messages_processed=10,
                start_time=start_time,
            )

        assert result.is_ok
        assert result.value.success is False
        assert result.value.session_id == "sess_123"
        assert result.value.execution_id == "exec_456"
        assert result.value.messages_processed == 10
        assert "cancelled" in result.value.final_message.lower()
        assert result.value.summary.get("cancelled") is True

    @pytest.mark.asyncio
    async def test_execute_seed_stops_on_cancellation(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Test that execute_seed detects cancellation and stops execution."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.events import create_session_cancelled_event
        from ouroboros.orchestrator.runner import CANCELLATION_CHECK_INTERVAL

        messages_yielded = 0

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            nonlocal messages_yielded
            # Yield enough messages to trigger a cancellation check
            for i in range(CANCELLATION_CHECK_INTERVAL + 5):
                messages_yielded += 1
                yield AgentMessage(type="assistant", content=f"Message {i}")
            # This final message should never be reached
            yield AgentMessage(
                type="result",
                content="Should not reach here",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        # Return no cancellation at startup, then return a cancellation event
        # at the first periodic message-loop checkpoint.
        cancel_event = create_session_cancelled_event("session_123", "User requested")
        # Cancellation now performs two fail-closed telemetry scans before the
        # terminal CAS and two more during legacy reconciliation.  Only the
        # session-cancelled poll should report the request; all acceptance
        # scans are empty for this fixture.
        mock_event_store.query_events = AsyncMock(side_effect=[[], [cancel_event], [], [], [], []])

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_cancelled(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_cancelled", mock_mark_cancelled),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        assert result.value.success is False
        assert "cancelled" in result.value.final_message.lower()
        # Should have stopped at the cancellation check interval
        assert result.value.messages_processed == CANCELLATION_CHECK_INTERVAL

    @pytest.mark.asyncio
    async def test_execute_seed_no_cancellation_proceeds_normally(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Test that execute_seed runs normally when no cancellation is issued."""
        from ouroboros.core.types import Result

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(type="tool", content="Reading", tool_name="Read")
            yield AgentMessage(
                type="result",
                content="Task completed successfully",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        # No cancellation events
        mock_event_store.query_events = AsyncMock(return_value=[])

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(
                SessionTracker.create(
                    str(kwargs["execution_id"]),
                    str(kwargs["seed_id"]),
                    session_id=str(kwargs["session_id"]),
                )
            )

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        assert result.value.success is True

    @pytest.mark.asyncio
    async def test_resume_session_stops_on_cancellation(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Test that resume_session detects cancellation and stops."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.events import create_session_cancelled_event
        from ouroboros.orchestrator.runner import CANCELLATION_CHECK_INTERVAL

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            for i in range(CANCELLATION_CHECK_INTERVAL + 5):
                yield AgentMessage(type="assistant", content=f"Message {i}")
            yield AgentMessage(
                type="result",
                content="Should not reach",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        cancel_event = create_session_cancelled_event("sess_resume_cancelled", "User requested")
        # The periodic cancellation poll sees the request; the subsequent
        # attempt/outcome scans are intentionally empty for this fixture.
        mock_event_store.query_events = AsyncMock(side_effect=[[cancel_event], [], []])

        running_tracker = SessionTracker.create("exec_resume", "seed_1").with_status(
            SessionStatus.PAUSED
        )
        running_tracker = _attach_live_process_local_contract(
            runner,
            running_tracker,
            sample_seed,
            session_id="sess_resume_cancelled",
        )

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(running_tracker)

        async def mock_mark_cancelled(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_cancelled", mock_mark_cancelled),
        ):
            result = await runner.resume_session("sess_resume_cancelled", sample_seed)

        assert result.is_ok
        assert result.value.success is False
        assert "cancelled" in result.value.final_message.lower()

    @pytest.mark.asyncio
    async def test_cancellation_check_interval_constant(self) -> None:
        """Test that CANCELLATION_CHECK_INTERVAL is defined and reasonable."""
        from ouroboros.orchestrator.runner import CANCELLATION_CHECK_INTERVAL

        assert isinstance(CANCELLATION_CHECK_INTERVAL, int)
        assert CANCELLATION_CHECK_INTERVAL > 0
        assert CANCELLATION_CHECK_INTERVAL <= 20  # Reasonable upper bound

    @pytest.mark.asyncio
    async def test_check_cancellation_detects_in_memory_registry(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test _check_cancellation returns True when session is in the in-memory registry."""
        from ouroboros.orchestrator.runner import (
            _cancellation_registry,
            clear_cancellation,
            request_cancellation,
        )

        # Ensure clean state
        _cancellation_registry.pop("sess_inmem", None)

        await request_cancellation("sess_inmem")
        try:
            # Should return True without even querying the event store
            result = await runner._check_cancellation("sess_inmem")
            assert result is True
            # Event store query should NOT have been called (fast path)
            mock_event_store.query_events.assert_not_called()
        finally:
            await clear_cancellation("sess_inmem")

    @pytest.mark.asyncio
    async def test_handle_cancellation_clears_in_memory_registry(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test _handle_cancellation clears the in-memory registry entry."""
        from datetime import UTC, datetime

        from ouroboros.orchestrator.runner import (
            is_cancellation_requested,
            request_cancellation,
        )

        await request_cancellation("sess_clear")

        with patch.object(runner._session_repo, "mark_cancelled", AsyncMock(return_value=None)):
            await runner._handle_cancellation(
                session_id="sess_clear",
                execution_id="exec_clear",
                messages_processed=5,
                start_time=datetime.now(UTC),
            )

        assert await is_cancellation_requested("sess_clear") is False


class TestCancellationRegistry:
    """Tests for the module-level in-memory cancellation registry functions."""

    def setup_method(self) -> None:
        """Clear the registry before each test."""
        from ouroboros.orchestrator.runner import _cancellation_registry

        _cancellation_registry.clear()

    def teardown_method(self) -> None:
        """Clear the registry after each test."""
        from ouroboros.orchestrator.runner import _cancellation_registry

        _cancellation_registry.clear()

    @pytest.mark.asyncio
    async def test_request_cancellation_adds_session(self) -> None:
        """Test that request_cancellation adds the session ID to the registry."""
        from ouroboros.orchestrator.runner import (
            is_cancellation_requested,
            request_cancellation,
        )

        assert await is_cancellation_requested("sess_1") is False
        await request_cancellation("sess_1")
        assert await is_cancellation_requested("sess_1") is True

    @pytest.mark.asyncio
    async def test_clear_cancellation_removes_session(self) -> None:
        """Test that clear_cancellation removes the session ID."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            is_cancellation_requested,
            request_cancellation,
        )

        await request_cancellation("sess_2")
        assert await is_cancellation_requested("sess_2") is True
        await clear_cancellation("sess_2")
        assert await is_cancellation_requested("sess_2") is False

    @pytest.mark.asyncio
    async def test_clear_cancellation_is_idempotent(self) -> None:
        """Test that clearing a non-existent session does not raise."""
        from ouroboros.orchestrator.runner import clear_cancellation

        # Should not raise
        await clear_cancellation("nonexistent_session")

    @pytest.mark.asyncio
    async def test_get_pending_cancellations_returns_frozenset(self) -> None:
        """Test that get_pending_cancellations returns a frozenset snapshot."""
        from ouroboros.orchestrator.runner import (
            get_pending_cancellations,
            request_cancellation,
        )

        await request_cancellation("sess_a")
        await request_cancellation("sess_b")

        pending = await get_pending_cancellations()
        assert isinstance(pending, frozenset)
        assert pending == frozenset({"sess_a", "sess_b"})

    @pytest.mark.asyncio
    async def test_get_pending_cancellations_is_snapshot(self) -> None:
        """Test that the returned frozenset is a snapshot, not a live view."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            get_pending_cancellations,
            request_cancellation,
        )

        await request_cancellation("sess_snap")
        snapshot = await get_pending_cancellations()
        await clear_cancellation("sess_snap")

        # Snapshot should still contain the session
        assert "sess_snap" in snapshot
        # But the registry should not
        new_snapshot = await get_pending_cancellations()
        assert "sess_snap" not in new_snapshot

    @pytest.mark.asyncio
    async def test_multiple_sessions_tracked_independently(self) -> None:
        """Test that multiple sessions can be tracked independently."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            is_cancellation_requested,
            request_cancellation,
        )

        await request_cancellation("sess_x")
        await request_cancellation("sess_y")

        assert await is_cancellation_requested("sess_x") is True
        assert await is_cancellation_requested("sess_y") is True

        await clear_cancellation("sess_x")
        assert await is_cancellation_requested("sess_x") is False
        assert await is_cancellation_requested("sess_y") is True

    @pytest.mark.asyncio
    async def test_request_cancellation_is_idempotent(self) -> None:
        """Test that requesting cancellation twice is safe."""
        from ouroboros.orchestrator.runner import (
            get_pending_cancellations,
            request_cancellation,
        )

        await request_cancellation("sess_dup")
        await request_cancellation("sess_dup")

        assert len(await get_pending_cancellations()) == 1


class TestExecutionCancelledError:
    """Tests for ExecutionCancelledError."""

    def test_create_with_defaults(self) -> None:
        """Test creating error with default reason."""
        from ouroboros.orchestrator.runner import ExecutionCancelledError

        error = ExecutionCancelledError(session_id="sess_123")
        assert error.session_id == "sess_123"
        assert error.reason == "Cancelled by user"
        assert "sess_123" in str(error)

    def test_create_with_custom_reason(self) -> None:
        """Test creating error with custom reason."""
        from ouroboros.orchestrator.runner import ExecutionCancelledError

        error = ExecutionCancelledError(session_id="sess_456", reason="Auto-cleanup: stale")
        assert error.session_id == "sess_456"
        assert error.reason == "Auto-cleanup: stale"
        assert "Auto-cleanup: stale" in str(error)
