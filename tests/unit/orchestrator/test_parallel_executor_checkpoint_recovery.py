"""RC3 checkpoint save/load keying and crash-restart recovery regressions.

Covers the two production gaps found by adversarial review on PR #1648:

1. ``getattr(seed, "id", session_id)`` always fell through to ``session_id``
   (``Seed`` has no ``id`` attribute — the stable identifier is
   ``seed.metadata.seed_id``), so a crash-restart run with a fresh session
   could never find the checkpoint the crashed run saved.
2. The RC3 recovery block never restored ``execution_id`` from the loaded
   checkpoint, so the ladder/attestation durable-state loaders
   (``_load_lateral_escalation_state`` / ``_load_decomposition_attestation``)
   replayed an empty event aggregate instead of the original run's events.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.dependency_analyzer import (
    ACNode,
    ExecutionStage,
    StagedExecutionPlan,
)
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelACExecutor
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import EventStore
from ouroboros.resilience.lateral import ThinkingPersona


def _seed(goal: str = "Checkpoint keying") -> Seed:
    return Seed(
        goal=goal,
        constraints=(),
        acceptance_criteria=("Parent work",),
        ontology_schema=OntologySchema(name="Checkpoint", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _plan() -> StagedExecutionPlan:
    return StagedExecutionPlan(
        nodes=(ACNode(index=0, content="Parent work"),),
        stages=(ExecutionStage(index=0, ac_indices=(0,)),),
    )


def _executor(
    *,
    event_store: object,
    checkpoint_store: CheckpointStore,
    **overrides: object,
) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=event_store,
        console=MagicMock(),
        checkpoint_store=checkpoint_store,
        cross_harness_redispatch=False,
        **overrides,  # type: ignore[arg-type]
    )


class TestCheckpointSeedIdKeying:
    """Fix 1: checkpoints must be keyed by ``seed.metadata.seed_id``."""

    def test_uses_seed_metadata_seed_id(self) -> None:
        seed = _seed()
        assert ParallelACExecutor._checkpoint_seed_id(seed, "session-live") == seed.metadata.seed_id
        # The Seed model genuinely has no ``id`` attribute — the old
        # ``getattr(seed, "id", session_id)`` pattern always fell through.
        assert not hasattr(seed, "id")

    def test_falls_back_to_session_id_without_metadata(self) -> None:
        bare = SimpleNamespace()
        assert ParallelACExecutor._checkpoint_seed_id(bare, "session-live") == "session-live"
        no_seed_id = SimpleNamespace(metadata=SimpleNamespace())
        assert ParallelACExecutor._checkpoint_seed_id(no_seed_id, "session-live") == "session-live"

    @pytest.mark.asyncio
    async def test_checkpoint_saved_under_stable_key_survives_new_session(
        self, tmp_path: Path
    ) -> None:
        """Save under session A, then load under a DIFFERENT session B.

        This is the crash-restart shape: the restarted run gets a fresh
        session_id, so only a seed-derived key lets recovery find the
        checkpoint the crashed run saved.
        """
        seed = _seed()
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        crashed_run = _executor(event_store=AsyncMock(), checkpoint_store=store)
        crashed_run._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
        )
        await crashed_run.execute_parallel(
            seed,
            session_id="session-original",
            execution_id="exec-original",
            tools=[],
            system_prompt="system",
            execution_plan=_plan(),
        )

        # The checkpoint must be retrievable by the stable seed id — NOT by
        # the (now-dead) original session id.
        loaded = store.load(seed.metadata.seed_id)
        assert loaded.is_ok
        assert loaded.value.state["execution_id"] == "exec-original"
        assert store.load("session-original").is_err


class TestCrashRestartRestoresExecutionId:
    """Fix 2: recovery must rejoin the ORIGINAL run's event aggregate.

    Simulated as close to a real crash-restart as this codebase's test
    infrastructure allows: a real file-backed ``CheckpointStore``, a real
    SQLite ``EventStore`` on disk, a crashed run that durably records
    mid-ladder escalation events under its execution_id before dying, and a
    genuinely FRESH executor instance (empty in-memory caches, fresh store
    handles) recovering with the new session/execution ids a restarted
    process would mint.
    """

    @pytest.mark.asyncio
    async def test_recovery_threads_original_execution_id_to_ladder_state(
        self, tmp_path: Path
    ) -> None:
        seed = Seed(
            goal="Crash-restart ladder durability",
            constraints=(),
            acceptance_criteria=("Parent work", "Verify integration"),
            ontology_schema=OntologySchema(name="Recovery", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )
        plan = StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="Parent work"),
                ACNode(index=1, content="Verify integration", depends_on=(0,)),
            ),
            stages=(
                ExecutionStage(index=0, ac_indices=(0,)),
                ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
            ),
        )
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"

        # --- Original run: level 1 completes (checkpoint saved with
        # execution_id "exec-original"), then the process dies mid-ladder
        # during level 2 — AFTER the ladder durably recorded its persona
        # streak, exactly the window emit_lateral_escalation_progressed
        # exists to cover.
        original_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await original_events.initialize()
        original_ckpt = CheckpointStore(base_path=ckpt_path)
        original_ckpt.initialize()
        original = _executor(event_store=original_events, checkpoint_store=original_ckpt)

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            execution_id = kwargs["execution_id"]
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            # Level 2: the ladder durably records in-flight escalation
            # progress under THIS run's execution_id, then the process
            # crashes before the redispatch finishes.
            node_id = ExecutionNodeIdentity.root(
                execution_context_id=str(execution_id), ac_index=1
            ).node_id
            persisted = await original._event_emitter.emit_lateral_escalation_progressed(
                execution_id=str(execution_id),
                session_id=str(kwargs["session_id"]),
                node_id=node_id,
                root_ac_index=1,
                personas_tried=("hacker", "researcher"),
                consecutive_terminal_failures=3,
                parked=False,
                persona="researcher",
                retry_attempt=7,
            )
            assert persisted
            raise RuntimeError("simulated process crash mid-ladder")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        # The anyio task group surfaces the crash wrapped in an
        # ExceptionGroup — unwrap before matching.
        with pytest.raises(BaseException) as crash_info:
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=plan,
            )

        def _flatten(exc: BaseException) -> list[BaseException]:
            if isinstance(exc, BaseExceptionGroup):
                return [leaf for sub in exc.exceptions for leaf in _flatten(sub)]
            return [exc]

        assert any("simulated process crash" in str(leaf) for leaf in _flatten(crash_info.value))
        await original_events.close()

        # Sanity: the crashed run left a level-1 checkpoint carrying its
        # execution_id, keyed by the stable seed id.
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["execution_id"] == "exec-original"
        assert saved.value.state["completed_levels"] == 1

        # --- Recovery run: a genuinely fresh process — new executor, new
        # store handles, and the NEW session/execution ids prepare_session
        # would mint for a restart.
        recovered_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await recovered_events.initialize()
        recovered = _executor(
            event_store=recovered_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        captured: dict[str, object] = {}

        async def _recovered_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            captured["execution_id"] = kwargs["execution_id"]
            captured["batch_executable"] = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            return [ACExecutionResult(ac_index=1, ac_content="Verify integration", success=True)]

        recovered._run_batch_with_verify_and_retry = AsyncMock(side_effect=_recovered_stage_runner)
        result = await recovered.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=[],
            system_prompt="system",
            execution_plan=plan,
        )

        # Recovery skipped the completed level and re-ran only level 2 —
        # under the ORIGINAL execution_id restored from the checkpoint.
        assert captured["batch_executable"] == [1]
        assert captured["execution_id"] == "exec-original"
        assert result.all_succeeded

        # The durable-state loader, keyed off the restored execution_id,
        # finds the REAL prior ladder events — not an empty replay.
        state = await recovered._load_lateral_escalation_state(
            1, execution_id=str(captured["execution_id"])
        )
        assert state.personas_tried == (ThinkingPersona.HACKER, ThinkingPersona.RESEARCHER)
        assert state.consecutive_terminal_failures == 3
        assert state.parked is False

        # Negative control: the naive (un-restored) fresh execution_id sees
        # an EMPTY aggregate — precisely the bug this regression guards.
        assert await recovered._replay_with_retry("execution", "exec-restarted") == []
        await recovered_events.close()


class TestCrashRestartRestoresRetryPolicy:
    """Round-9 Finding #2 (BLOCKING): RC3 recovery consulted durable
    ladder/escalation history only when the CURRENTLY-constructed executor's
    ``lateral_escalation_enabled`` was True — but the checkpoint never
    persisted the ORIGINAL run's retry policy. A restart under a different
    config (the default False, or an operator edit between crash and
    restart) treated a genuinely parked/mid-ladder AC as having no ladder
    history at all: fresh retry budget, escalation never reached, FAILED
    surfaced once the budget was spent. Recovery must keep the policy the
    run STARTED with, exactly like the runner's durable retry_policy
    contract does for resume_session."""

    @pytest.mark.asyncio
    async def test_parked_ac_history_honored_when_current_config_disables_escalation(
        self, tmp_path: Path
    ) -> None:
        seed = Seed(
            goal="Crash-restart policy durability",
            constraints=(),
            acceptance_criteria=("Parent work", "Verify integration"),
            ontology_schema=OntologySchema(name="Recovery", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )
        plan = StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="Parent work"),
                ACNode(index=1, content="Verify integration", depends_on=(0,)),
            ),
            stages=(
                ExecutionStage(index=0, ac_indices=(0,)),
                ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
            ),
        )
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"

        # --- Original run: escalation ENABLED with a distinctive policy.
        # Level 1 completes (checkpoint saved), then the process dies with
        # AC 1 durably PARKED mid-escalation.
        original_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await original_events.initialize()
        original_ckpt = CheckpointStore(base_path=ckpt_path)
        original_ckpt.initialize()
        original = _executor(
            event_store=original_events,
            checkpoint_store=original_ckpt,
            lateral_escalation_enabled=True,
            parked_retry_backoff_seconds=1234.5,
            ac_retry_attempts=5,
        )

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            execution_id = str(kwargs["execution_id"])
            node_id = ExecutionNodeIdentity.root(
                execution_context_id=execution_id, ac_index=1
            ).node_id
            all_personas = tuple(p.value for p in ThinkingPersona)
            assert await original._event_emitter.emit_lateral_escalation_progressed(
                execution_id=execution_id,
                session_id=str(kwargs["session_id"]),
                node_id=node_id,
                root_ac_index=1,
                personas_tried=all_personas,
                consecutive_terminal_failures=9,
                parked=True,
                persona=None,
                retry_attempt=7,
            )
            assert await original._event_emitter.emit_ac_parked_for_operator(
                execution_id=execution_id,
                session_id=str(kwargs["session_id"]),
                node_id=node_id,
                root_ac_index=1,
                personas_tried=all_personas,
                consecutive_terminal_failures=9,
                backoff_seconds=1234.5,
                reason="all lateral-thinking personas exhausted",
            )
            raise RuntimeError("simulated process crash while parked")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        with pytest.raises(BaseException):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=plan,
            )
        await original_events.close()

        # Sanity: the checkpoint carries the ORIGINAL run's retry policy.
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["retry_policy"] == {
            "lateral_escalation_enabled": True,
            "parked_retry_backoff_seconds": 1234.5,
            "ac_retry_attempts": 5,
        }

        # --- Restart under a DIFFERENT config: the fresh executor is
        # constructed with escalation DISABLED (the default posture).
        recovered_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await recovered_events.initialize()
        recovered = _executor(
            event_store=recovered_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        assert recovered._lateral_escalation_enabled is False
        recovered._sleep = AsyncMock()
        dispatch_calls: list[dict[str, object]] = []

        async def _succeeds(**kwargs: object) -> list[ACExecutionResult]:
            dispatch_calls.append(kwargs)
            return [ACExecutionResult(ac_index=1, ac_content="Verify integration", success=True)]

        recovered._execute_ac_batch = AsyncMock(side_effect=_succeeds)
        recovered._apply_verify_gate = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])

        result = await recovered.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=[],
            system_prompt="system",
            execution_plan=plan,
        )

        # The checkpointed policy was restored wholesale: recovery keeps the
        # termination semantics the run started with, not the current config.
        assert recovered._lateral_escalation_enabled is True
        assert recovered._parked_retry_backoff_seconds == 1234.5
        assert recovered._ac_retry_attempts == 5
        # The parked AC's durable history was consulted and honored: exactly
        # ONE dispatch through the resumed-ladder re-entry (post-budget, max
        # strength, parked cadence slept first) — never a fresh retry budget.
        assert result.all_succeeded
        assert len(dispatch_calls) == 1
        assert dispatch_calls[0]["same_runtime_budget_exhausted"] is True
        assert dispatch_calls[0]["force_frontier_routing"] is True
        recovered._sleep.assert_any_await(1234.5)
        # The breakthrough resolved the episode durably under the ORIGINAL
        # execution_id's aggregate.
        node_id = ExecutionNodeIdentity.root(
            execution_context_id="exec-original", ac_index=1
        ).node_id
        events = await recovered_events.replay("execution", "exec-original")
        assert any(
            event.type == "execution.ac.parked_resolved" and event.data.get("node_id") == node_id
            for event in events
        )
        await recovered_events.close()

    def test_legacy_checkpoint_without_policy_keeps_current_config(self) -> None:
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._restore_checkpoint_retry_policy(None)
        assert executor._lateral_escalation_enabled is False

    def test_malformed_policy_fails_closed_to_honoring_history(self) -> None:
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        before_backoff = executor._parked_retry_backoff_seconds
        before_attempts = executor._ac_retry_attempts
        executor._restore_checkpoint_retry_policy(
            {"lateral_escalation_enabled": "yes", "parked_retry_backoff_seconds": 0}
        )
        # Corruption fails closed in the direction the escalation mandate
        # demands: durable history will be consulted (never silently
        # dropped), while the unvalidatable numeric values are not adopted.
        assert executor._lateral_escalation_enabled is True
        assert executor._parked_retry_backoff_seconds == before_backoff
        assert executor._ac_retry_attempts == before_attempts
