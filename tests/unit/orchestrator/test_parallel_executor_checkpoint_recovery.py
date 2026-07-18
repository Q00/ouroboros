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
from ouroboros.orchestrator.model_routing import ModelRouter, serialize_model_router
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
        # Round-10 finding #3: the restored id is propagated back through
        # the result so the CALLER can rejoin its own terminal-status and
        # frugality bookkeeping to the same aggregate.
        assert result.execution_id == "exec-original"

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
            # Round-11 finding #2: the round-9 #4 execution-semantic trio
            # must ride the checkpoint too — distinctive values so the
            # restart (which defaults to None/True/600) proves restoration.
            reasoning_effort="xhigh",
            run_verify_commands=False,
            verify_command_timeout_seconds=77,
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
            "reasoning_effort": "xhigh",
            "run_verify_commands": False,
            "verify_command_timeout_seconds": 77,
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
        assert recovered._reasoning_effort is None
        assert recovered._run_verify_commands is True
        assert recovered._verify_command_timeout_seconds == 600
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
        # Round-11 finding #2: the execution-semantic trio too — the restored
        # level runs under the ORIGINAL run's effort/verify-gate semantics.
        assert recovered._reasoning_effort == "xhigh"
        assert recovered._run_verify_commands is False
        assert recovered._verify_command_timeout_seconds == 77
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


class TestStaleCheckpointFromFinishedRunNeverResumed:
    """Round-10 finding #1 (BLOCKING): the checkpoint key is the bare
    ``seed.metadata.seed_id`` — no run-generation discriminator — so a
    SECOND, entirely new run of the same Seed found the FIRST run's
    COMPLETED checkpoint, adopted it, skipped every level, and silently
    reported success without dispatching a single AC. A checkpoint must be
    a resume ticket for an INTERRUPTED run only: (a) the runner deletes it
    once the run records a non-resumable terminal outcome, and (b) the
    executor's recovery gate refuses any leftover checkpoint whose run
    already recorded ``completed``/``failed``/``cancelled`` in its durable
    execution aggregate. ``paused`` — the resumable state — keeps both the
    checkpoint and the recovery semantics."""

    @pytest.mark.asyncio
    async def test_fresh_rerun_after_completed_run_dispatches_acs(self, tmp_path: Path) -> None:
        """The review's exact probe, end to end at the executor boundary.

        Run 1 completes (checkpoint says every level is done) and the
        runner's durable terminal record lands. Run 2 is a genuinely NEW
        execution of the same seed — new session_id, new execution_id, no
        resume intent. It must dispatch the ACs from scratch under its OWN
        execution_id, never silently succeed off the finished run's
        checkpoint.
        """
        from ouroboros.orchestrator.events import create_execution_terminal_event

        seed = _seed("Fresh re-run after completed run")
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"

        # --- Run 1: completes normally; its final checkpoint marks the
        # whole plan done.
        run1_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run1_events.initialize()
        run1_ckpt = CheckpointStore(base_path=ckpt_path)
        run1_ckpt.initialize()
        run1 = _executor(event_store=run1_events, checkpoint_store=run1_ckpt)
        run1._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
        )
        result1 = await run1.execute_parallel(
            seed,
            session_id="session-original",
            execution_id="exec-original",
            tools=[],
            system_prompt="system",
            execution_plan=_plan(),
        )
        assert result1.all_succeeded
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["completed_levels"] == 1

        # The runner mirrors the terminal outcome into the durable
        # execution aggregate (this is the staleness discriminator).
        await run1_events.append(
            create_execution_terminal_event(
                execution_id="exec-original",
                session_id="session-original",
                status="completed",
            )
        )
        await run1_events.close()

        # --- Run 2: a genuinely fresh execution of the SAME seed.
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        captured: dict[str, object] = {}

        async def _fresh_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            captured["execution_id"] = kwargs["execution_id"]
            captured["batch_executable"] = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]

        run2._run_batch_with_verify_and_retry = AsyncMock(side_effect=_fresh_stage_runner)
        result2 = await run2.execute_parallel(
            seed,
            session_id="session-rerun",
            execution_id="exec-rerun",
            tools=[],
            system_prompt="system",
            execution_plan=_plan(),
        )

        # The ACs actually ran — never skipped off the stale checkpoint —
        # and under the NEW run's own execution_id, not the finished run's.
        assert captured["batch_executable"] == [0]
        assert captured["execution_id"] == "exec-rerun"
        assert result2.all_succeeded
        assert result2.execution_id == "exec-rerun"
        assert all(r.final_message != "[Restored from checkpoint]" for r in result2.results)
        # The stale checkpoint was replaced by run 2's own (fresh) one.
        replaced = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert replaced.is_ok
        assert replaced.value.state["execution_id"] == "exec-rerun"
        await run2_events.close()

    @pytest.mark.asyncio
    async def test_paused_run_checkpoint_remains_resumable(self, tmp_path: Path) -> None:
        """``paused`` is the resumable state: the recovery gate must keep
        honoring its checkpoint (execution_id restored, completed levels
        skipped) — this is the escalation/parked recovery flow itself."""
        from ouroboros.orchestrator.events import create_execution_terminal_event

        seed = Seed(
            goal="Paused run stays resumable",
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

        run1_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run1_events.initialize()
        run1_ckpt = CheckpointStore(base_path=ckpt_path)
        run1_ckpt.initialize()
        run1 = _executor(event_store=run1_events, checkpoint_store=run1_ckpt)

        async def _pausing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            raise RuntimeError("simulated interruption after level 1")

        run1._run_batch_with_verify_and_retry = AsyncMock(side_effect=_pausing_stage_runner)
        with pytest.raises(BaseException):
            await run1.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=plan,
            )
        # The run's terminal record says PAUSED — a resumable outcome.
        await run1_events.append(
            create_execution_terminal_event(
                execution_id="exec-original",
                session_id="session-original",
                status="paused",
                pause_seconds=600,
            )
        )
        await run1_events.close()

        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        captured: dict[str, object] = {}

        async def _resumed_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            captured["execution_id"] = kwargs["execution_id"]
            captured["batch_executable"] = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            return [ACExecutionResult(ac_index=1, ac_content="Verify integration", success=True)]

        run2._run_batch_with_verify_and_retry = AsyncMock(side_effect=_resumed_stage_runner)
        result = await run2.execute_parallel(
            seed,
            session_id="session-resumed",
            execution_id="exec-resumed",
            tools=[],
            system_prompt="system",
            execution_plan=plan,
        )

        # Genuine resume semantics preserved: completed level skipped and
        # the ORIGINAL execution_id restored — and reported back to the
        # caller for its own terminal/frugality bookkeeping (finding #3).
        assert captured["batch_executable"] == [1]
        assert captured["execution_id"] == "exec-original"
        assert result.all_succeeded
        assert result.execution_id == "exec-original"
        await run2_events.close()


def _make_runner(tmp_path: Path) -> tuple[object, CheckpointStore, object]:
    from ouroboros.orchestrator.runner import OrchestratorRunner

    store = CheckpointStore(base_path=tmp_path / "checkpoints")
    store.initialize()
    adapter = MagicMock()
    adapter.runtime_backend = "opencode"
    adapter.working_directory = "/tmp/project"
    adapter.permission_mode = "acceptEdits"
    event_store = AsyncMock()
    event_store.append = AsyncMock()
    event_store.replay = AsyncMock(return_value=[])
    runner = OrchestratorRunner(adapter, event_store, MagicMock(), checkpoint_store=store)
    return runner, store, event_store


class TestRunnerDeletesCheckpointAtTerminal:
    """Runner half of round-10 finding #1: once a run records a
    non-resumable terminal outcome (completed/failed), its checkpoint must
    not survive to be adopted by a later fresh run. ``paused`` keeps its
    checkpoint — that is the state the resume flow exists for."""

    def _runner_harness(self, tmp_path: Path) -> tuple[object, CheckpointStore, object]:
        return _make_runner(tmp_path)

    def _seed_checkpoint(self, store: CheckpointStore, seed: Seed) -> None:
        from ouroboros.persistence.checkpoint import CheckpointData

        save_result = store.save(
            CheckpointData.create(
                seed_id=seed.metadata.seed_id,
                phase="parallel_execution",
                state={
                    "session_id": "session-prior",
                    "execution_id": "exec-prior",
                    "completed_levels": 1,
                    "ac_statuses": {"0": "completed"},
                    "failed_indices": [],
                    "completed_count": 1,
                },
            )
        )
        assert save_result.is_ok

    async def _run_parallel(
        self,
        runner: object,
        seed: Seed,
        parallel_result: object,
        *,
        pause: object = None,
    ) -> object:
        from unittest.mock import patch

        from ouroboros.core.types import Result
        from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
        from ouroboros.orchestrator.session import SessionTracker

        tracker = SessionTracker.create("exec-fresh", seed.metadata.seed_id)
        dependency_graph = DependencyGraph(
            nodes=(ACNode(index=0, content="Parent work"),),
            execution_levels=((0,),),
        )
        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner,
                "_recoverable_failure_pause_from_parallel_result",
                MagicMock(return_value=pause),
            ),
            patch.object(
                runner._session_repo, "mark_completed", AsyncMock(return_value=Result.ok(None))
            ),
            patch.object(
                runner._session_repo, "mark_failed", AsyncMock(return_value=Result.ok(None))
            ),
            patch.object(
                runner._session_repo, "mark_paused", AsyncMock(return_value=Result.ok(None))
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                AsyncMock(return_value=parallel_result),
            ),
        ):
            return await runner._execute_parallel(
                seed=seed,
                exec_id="exec-fresh",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

    @pytest.mark.asyncio
    async def test_completed_run_deletes_checkpoint(self, tmp_path: Path) -> None:
        from ouroboros.orchestrator.parallel_executor import ParallelExecutionResult

        seed = _seed("Terminal completion clears checkpoint")
        runner, store, _ = self._runner_harness(tmp_path)
        self._seed_checkpoint(store, seed)

        result = await self._run_parallel(
            runner,
            seed,
            ParallelExecutionResult(
                results=(ACExecutionResult(ac_index=0, ac_content="Parent work", success=True),),
                success_count=1,
                failure_count=0,
            ),
        )

        assert result.is_ok
        assert store.load(seed.metadata.seed_id).is_err

    @pytest.mark.asyncio
    async def test_failed_run_deletes_checkpoint(self, tmp_path: Path) -> None:
        from ouroboros.orchestrator.parallel_executor import ParallelExecutionResult

        seed = _seed("Terminal failure clears checkpoint")
        runner, store, _ = self._runner_harness(tmp_path)
        self._seed_checkpoint(store, seed)

        result = await self._run_parallel(
            runner,
            seed,
            ParallelExecutionResult(
                results=(
                    ACExecutionResult(
                        ac_index=0,
                        ac_content="Parent work",
                        success=False,
                        error="terminal failure",
                    ),
                ),
                success_count=0,
                failure_count=1,
            ),
        )

        assert result.is_ok
        assert store.load(seed.metadata.seed_id).is_err

    @pytest.mark.asyncio
    async def test_paused_run_keeps_checkpoint(self, tmp_path: Path) -> None:
        from ouroboros.orchestrator.parallel_executor import ParallelExecutionResult
        from ouroboros.orchestrator.runner import RecoverableFailurePause

        seed = _seed("Paused run keeps checkpoint")
        runner, store, _ = self._runner_harness(tmp_path)
        self._seed_checkpoint(store, seed)

        result = await self._run_parallel(
            runner,
            seed,
            ParallelExecutionResult(
                results=(
                    ACExecutionResult(
                        ac_index=0,
                        ac_content="Parent work",
                        success=False,
                        error="recoverable failure",
                    ),
                ),
                success_count=0,
                failure_count=1,
            ),
            pause=RecoverableFailurePause(
                reason="rate limited",
                resume_hint="retry later",
                pause_seconds=600,
                resume_after=None,
                pause_kind="rate_limit",
            ),
        )

        assert result.is_ok
        assert store.load(seed.metadata.seed_id).is_ok


class TestCrashRestartRestoresRetryPolicyLegacyAndMalformed:
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


class TestCrashRestartRestoresExecutionSemanticFields:
    """Round-11 finding #2 (BLOCKING): round 9's #4 fix added
    ``reasoning_effort``, ``run_verify_commands``, and
    ``verify_command_timeout_seconds`` to the RUNNER-level durable
    retry-policy contract (session-resume path) because the
    ladder/attestation state machine depends on them — but the
    CHECKPOINT-level RC3 restore was never updated to match, so a
    crash-restart recovering through the executor's own checkpoint could
    execute the restored level under DIFFERENT effort/verify-gate semantics
    than the original run. The checkpoint now persists and restores the
    trio with the same validation rules as the runner contract."""

    @staticmethod
    def _executor(**overrides: object) -> ParallelACExecutor:
        return ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            **overrides,  # type: ignore[arg-type]
        )

    _VALID_LEGACY = {
        "lateral_escalation_enabled": True,
        "parked_retry_backoff_seconds": 45.0,
        "ac_retry_attempts": 3,
    }

    def test_original_semantics_win_over_current_config(self) -> None:
        """A checkpoint carrying a DIFFERENT effort/verify-gate trio than
        the current process config must restore the ORIGINAL values."""
        executor = self._executor(
            reasoning_effort="low",
            run_verify_commands=True,
            verify_command_timeout_seconds=600,
        )
        executor._restore_checkpoint_retry_policy(
            {
                **self._VALID_LEGACY,
                "reasoning_effort": "xhigh",
                "run_verify_commands": False,
                "verify_command_timeout_seconds": 77,
            }
        )
        assert executor._reasoning_effort == "xhigh"
        assert executor._run_verify_commands is False
        assert executor._verify_command_timeout_seconds == 77
        assert executor._lateral_escalation_enabled is True
        assert executor._parked_retry_backoff_seconds == 45.0
        assert executor._ac_retry_attempts == 3

    def test_dormant_effort_none_is_restored_over_configured_effort(self) -> None:
        """``None`` is a legitimate persisted value (dormant effort axis) and
        must override a currently-configured effort — distinguishable from a
        merely ABSENT key."""
        executor = self._executor(reasoning_effort="high")
        executor._restore_checkpoint_retry_policy(
            {
                **self._VALID_LEGACY,
                "reasoning_effort": None,
                "run_verify_commands": True,
                "verify_command_timeout_seconds": 600,
            }
        )
        assert executor._reasoning_effort is None

    def test_legacy_trio_policy_migrates_new_fields_from_current_config(self) -> None:
        """A checkpoint written before this fix carries only the legacy three
        fields: restore those, and keep the CURRENT config's values for the
        absent trio (one-time migration, mirroring the policy-is-None
        posture) — never treat the absence as corruption."""
        executor = self._executor(
            reasoning_effort="medium",
            run_verify_commands=False,
            verify_command_timeout_seconds=120,
        )
        executor._restore_checkpoint_retry_policy(dict(self._VALID_LEGACY))
        assert executor._lateral_escalation_enabled is True
        assert executor._parked_retry_backoff_seconds == 45.0
        assert executor._ac_retry_attempts == 3
        assert executor._reasoning_effort == "medium"
        assert executor._run_verify_commands is False
        assert executor._verify_command_timeout_seconds == 120

    @pytest.mark.parametrize(
        "corruption",
        [
            {"reasoning_effort": "ultra"},
            {"reasoning_effort": ["low"]},
            {"run_verify_commands": "yes"},
            {"verify_command_timeout_seconds": 0},
            {"verify_command_timeout_seconds": True},
            {"verify_command_timeout_seconds": 60.0},
        ],
    )
    def test_malformed_new_field_fails_closed(self, corruption: dict[str, object]) -> None:
        """A PRESENT-but-malformed value in any of the trio takes the whole
        policy down the established fail-closed branch: escalation gates
        forced open (durable history will be honored), current config's
        validated values kept for everything that could not be restored."""
        executor = self._executor(
            reasoning_effort="high",
            run_verify_commands=True,
            verify_command_timeout_seconds=600,
        )
        executor._restore_checkpoint_retry_policy(
            {
                "lateral_escalation_enabled": False,
                "parked_retry_backoff_seconds": 45.0,
                "ac_retry_attempts": 3,
                "reasoning_effort": "low",
                "run_verify_commands": False,
                "verify_command_timeout_seconds": 30,
                **corruption,
            }
        )
        assert executor._lateral_escalation_enabled is True
        assert executor._reasoning_effort == "high"
        assert executor._run_verify_commands is True
        assert executor._verify_command_timeout_seconds == 600


def _router(**overrides: object) -> ModelRouter:
    defaults: dict[str, object] = {
        "tier_models": {
            "frugal": "claude-haiku-test",
            "standard": "claude-sonnet-test",
            "frontier": "claude-opus-test",
        },
        "runtime_backend": "claude",
        "child_tier": "frugal",
        "base_tier": "standard",
        "escalation_retry_threshold": 1,
    }
    defaults.update(overrides)
    return ModelRouter(**defaults)  # type: ignore[arg-type]


class TestCrashRestartRestoresModelRouterAndExecutionSemantics:
    """Round-12 finding #2 (BLOCKING): rounds 9/11 taught the RC3 checkpoint
    to persist/restore the retry-policy scalars, but ``self._model_router``
    (governs actual model-tier routing during ladder dispatch and the
    frugality-proof cohort identity) and the constructor-injected
    dispatch/verification scalars (``decomposition_mode`` — whether
    decomposition even runs — plus ``max_decomposition_depth``,
    ``fat_harness_mode``, ``cross_harness_redispatch_enabled``,
    ``shadow_replay_enabled``) were still only ever set in ``__init__``.
    A crash-restart recovery therefore silently adopted the CURRENT
    process's routing and execution mode instead of the ORIGINAL run's.
    The checkpoint now persists both — the router in the SAME versioned
    ``serialize_model_router`` contract the runner-level session-resume
    path already uses — and recovery restores them with the established
    fail-closed conventions."""

    @pytest.mark.asyncio
    async def test_recovery_restores_original_router_and_semantics(self, tmp_path: Path) -> None:
        """(a)+(b) end to end: the original run crashes after level 1 with a
        DISTINCTIVE router + execution semantics; the restart process is
        constructed with a DIFFERENT router and mode. The recovered level
        must dispatch under the ORIGINAL run's router and semantics."""
        seed = Seed(
            goal="Crash-restart router durability",
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
        original_router = _router()

        original_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await original_events.initialize()
        original_ckpt = CheckpointStore(base_path=ckpt_path)
        original_ckpt.initialize()
        original = _executor(
            event_store=original_events,
            checkpoint_store=original_ckpt,
            model_router=original_router,
            decomposition_mode="bounce_only",
            max_decomposition_depth=1,
            fat_harness_mode=True,
            shadow_replay_enabled=True,
        )

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            raise RuntimeError("simulated process crash after level 1")

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

        # Sanity: the checkpoint carries the versioned routing contract and
        # the execution-semantic scalars the run started with.
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["model_routing"] == serialize_model_router(original_router)
        assert saved.value.state["execution_semantics"] == {
            "decomposition_mode": "bounce_only",
            "max_decomposition_depth": 1,
            "fat_harness_mode": True,
            "cross_harness_redispatch_enabled": False,
            "shadow_replay_enabled": True,
        }

        # --- Restart under DIFFERENT construction args: another router
        # (different tier models / starting tiers) and the default
        # "preflight" mode.
        recovered_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await recovered_events.initialize()
        recovered = _executor(
            event_store=recovered_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            model_router=_router(
                tier_models={"frontier": "gpt-frontier-test"},
                runtime_backend="codex_cli",
                child_tier="frontier",
                base_tier="frontier",
                escalation_retry_threshold=2,
            ),
        )
        captured: dict[str, object] = {}

        async def _recovered_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            # Snapshot the executor's live routing/semantics AT dispatch
            # time: this is what actually governs the recovered level.
            captured["router"] = recovered._model_router
            captured["decomposition_mode"] = recovered._decomposition_mode
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

        assert result.all_succeeded
        # The recovered level dispatched under the ORIGINAL run's router —
        # its tier->model map and starting tiers, not the restart's.
        assert captured["router"] == original_router
        assert captured["decomposition_mode"] == "bounce_only"
        assert recovered._model_router == original_router
        assert recovered._decomposition_mode == "bounce_only"
        assert recovered._enable_decomposition is True
        assert recovered._max_decomposition_depth == 1
        assert recovered._fat_harness_mode is True
        assert recovered._shadow_replay_enabled is True
        await recovered_events.close()

    def _unit_executor(self, **overrides: object) -> ParallelACExecutor:
        return ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            cross_harness_redispatch=False,
            **overrides,  # type: ignore[arg-type]
        )

    def test_dormant_router_contract_is_restored_over_current_router(self) -> None:
        """``enabled=False`` is a real persisted contract (kill-switched
        run), distinct from a legacy checkpoint with no contract at all: it
        must override a currently-constructed router, keeping the resumed
        run dormant."""
        executor = self._unit_executor(model_router=_router())
        executor._restore_checkpoint_model_router(serialize_model_router(None))
        assert executor._model_router is None
        assert executor._lateral_escalation_enabled is False

    def test_legacy_checkpoint_without_router_keeps_current_router(self) -> None:
        current = _router()
        executor = self._unit_executor(model_router=current)
        executor._restore_checkpoint_model_router(None)
        assert executor._model_router == current
        assert executor._lateral_escalation_enabled is False

    @pytest.mark.parametrize(
        "corruption",
        [
            "not-a-mapping",
            {"version": 99, "enabled": True},
            {"version": 1, "enabled": "yes"},
            {"version": 1, "enabled": True, "router": {"tier_models": {}}},
            {
                "version": 1,
                "enabled": True,
                "router": {
                    "tier_models": {"frugal": "m"},
                    "runtime_backend": "claude",
                    "child_tier": "ultra",
                    "base_tier": "frugal",
                    "escalation_retry_threshold": 1,
                },
            },
        ],
    )
    def test_malformed_router_contract_fails_closed(self, corruption: object) -> None:
        """(c) corruption/tampering: escalation gates forced open (durable
        ladder history must be honored) while the CURRENT router is kept
        for actual dispatch — mirrors the malformed retry-policy branch."""
        current = _router()
        executor = self._unit_executor(model_router=current)
        executor._restore_checkpoint_model_router(corruption)
        assert executor._model_router == current
        assert executor._lateral_escalation_enabled is True

    def test_legacy_checkpoint_without_semantics_keeps_current_config(self) -> None:
        executor = self._unit_executor(decomposition_mode="preflight")
        executor._restore_checkpoint_execution_semantics(None)
        assert executor._decomposition_mode == "preflight"
        assert executor._lateral_escalation_enabled is False

    def test_absent_semantics_keys_migrate_from_current_config(self) -> None:
        """A partial mapping (forward-compat migration shape) restores only
        the present keys and keeps the current value for absent ones."""
        executor = self._unit_executor(decomposition_mode="preflight", fat_harness_mode=True)
        executor._restore_checkpoint_execution_semantics({"decomposition_mode": "off"})
        assert executor._decomposition_mode == "off"
        assert executor._enable_decomposition is False
        assert executor._fat_harness_mode is True
        assert executor._lateral_escalation_enabled is False

    @pytest.mark.parametrize(
        "corruption",
        [
            "not-a-mapping",
            {"decomposition_mode": "sideways"},
            {"decomposition_mode": None},
            {"max_decomposition_depth": -1},
            {"max_decomposition_depth": True},
            {"max_decomposition_depth": 2.0},
            {"fat_harness_mode": "yes"},
            {"cross_harness_redispatch_enabled": 1},
            {"shadow_replay_enabled": "on"},
        ],
    )
    def test_malformed_semantics_fails_closed(self, corruption: object) -> None:
        """(c) any present-but-malformed key takes the whole mapping down
        the established fail-closed branch: escalation gates forced open,
        nothing adopted."""
        executor = self._unit_executor(
            decomposition_mode="preflight",
            max_decomposition_depth=3,
            fat_harness_mode=False,
            shadow_replay_enabled=False,
        )
        valid = {
            "decomposition_mode": "bounce_only",
            "max_decomposition_depth": 1,
            "fat_harness_mode": True,
            "cross_harness_redispatch_enabled": True,
            "shadow_replay_enabled": True,
        }
        payload: object = (
            corruption if not isinstance(corruption, dict) else {**valid, **corruption}
        )
        executor._restore_checkpoint_execution_semantics(payload)
        assert executor._decomposition_mode == "preflight"
        assert executor._enable_decomposition is True
        assert executor._max_decomposition_depth == 3
        assert executor._fat_harness_mode is False
        assert executor._cross_harness_redispatch_enabled is False
        assert executor._shadow_replay_enabled is False
        assert executor._lateral_escalation_enabled is True


class TestRunnerAdoptsRecoveredExecutionId:
    """Round-10 finding #3 (BLOCKING): RC3 recovery restores the ORIGINAL
    run's execution_id inside the executor, so every AC event of the
    continuation lands under that aggregate — but the runner kept its OWN
    freshly-minted id and used it for post-execution bookkeeping. One
    logical run was split across two execution aggregates: AC events under
    the old id; terminal status, frugality proof, and the returned
    OrchestratorResult under the new one. The executor now returns the id
    it actually emitted under (``ParallelExecutionResult.execution_id``)
    and the runner must adopt it for ALL execution-scoped bookkeeping."""

    async def _run(
        self,
        runner: object,
        seed: Seed,
        parallel_result: object,
    ) -> tuple[object, object, AsyncMock, AsyncMock]:
        from unittest.mock import patch

        from ouroboros.core.types import Result
        from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
        from ouroboros.orchestrator.session import SessionTracker

        tracker = SessionTracker.create("exec-fresh", seed.metadata.seed_id)
        # The session registry is keyed under the FRESH id at registration
        # time (execute_precreated_session does this before execution).
        runner._active_sessions["exec-fresh"] = tracker.session_id
        dependency_graph = DependencyGraph(
            nodes=(ACNode(index=0, content="Parent work"),),
            execution_levels=((0,),),
        )
        proof = AsyncMock()
        retrospective = AsyncMock(return_value=True)
        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(runner, "_evaluate_frugality_proof", proof),
            patch.object(runner, "_report_frugality_retrospective", retrospective),
            patch.object(
                runner._session_repo, "mark_completed", AsyncMock(return_value=Result.ok(None))
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                AsyncMock(return_value=parallel_result),
            ),
        ):
            result = await runner._execute_parallel(
                seed=seed,
                exec_id="exec-fresh",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )
        return result, tracker, proof, retrospective

    @staticmethod
    def _terminal_events(event_store: AsyncMock) -> list[object]:
        return [
            call.args[0]
            for call in event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "execution.terminal"
        ]

    @pytest.mark.asyncio
    async def test_terminal_and_frugality_follow_restored_execution_id(
        self, tmp_path: Path
    ) -> None:
        """Simulated crash-recovery: the executor reports the checkpoint-
        restored id ("exec-original") while the runner minted "exec-fresh".
        Terminal-status recording, frugality-proof evaluation, and the
        returned OrchestratorResult must ALL follow the restored id — no
        split-aggregate mismatch between AC-level and session-level
        bookkeeping."""
        from ouroboros.orchestrator.parallel_executor import ParallelExecutionResult

        seed = _seed("Recovered execution id adoption")
        runner, _store, event_store = _make_runner(tmp_path)

        result, tracker, proof, retrospective = await self._run(
            runner,
            seed,
            ParallelExecutionResult(
                results=(ACExecutionResult(ac_index=0, ac_content="Parent work", success=True),),
                success_count=1,
                failure_count=0,
                execution_id="exec-original",
            ),
        )

        assert result.is_ok
        # (b) Terminal status lands on the ORIGINAL aggregate the AC events
        # (a) were emitted under.
        terminal_events = self._terminal_events(event_store)
        assert len(terminal_events) == 1
        assert terminal_events[0].aggregate_id == "exec-original"
        assert terminal_events[0].data["status"] == "completed"
        # (b) Frugality proof + retrospective are evaluated over the SAME
        # restored aggregate, never the stale fresh id.
        proof.assert_awaited_once_with("exec-original")
        retrospective.assert_awaited_once_with(
            execution_id="exec-original",
            session_id=tracker.session_id,
            terminal_status="completed",
        )
        # Downstream consumers get the id the events actually live under.
        assert result.value.execution_id == "exec-original"
        # In-memory session tracking was registered under the FRESH id and
        # must be cleaned up under that SAME key — no leaked entry.
        assert "exec-fresh" not in runner._active_sessions
        assert "exec-original" not in runner._active_sessions

    @pytest.mark.asyncio
    async def test_without_recovery_fresh_execution_id_is_kept(self, tmp_path: Path) -> None:
        """No recovery happened: the executor echoes the caller's own id
        back and every piece of bookkeeping stays on it (guard against the
        adoption logic changing non-recovery behavior)."""
        from ouroboros.orchestrator.parallel_executor import ParallelExecutionResult

        seed = _seed("No recovery keeps fresh id")
        runner, _store, event_store = _make_runner(tmp_path)

        result, tracker, proof, retrospective = await self._run(
            runner,
            seed,
            ParallelExecutionResult(
                results=(ACExecutionResult(ac_index=0, ac_content="Parent work", success=True),),
                success_count=1,
                failure_count=0,
                execution_id="exec-fresh",
            ),
        )

        assert result.is_ok
        terminal_events = self._terminal_events(event_store)
        assert len(terminal_events) == 1
        assert terminal_events[0].aggregate_id == "exec-fresh"
        proof.assert_awaited_once_with("exec-fresh")
        retrospective.assert_awaited_once_with(
            execution_id="exec-fresh",
            session_id=tracker.session_id,
            terminal_status="completed",
        )
        assert result.value.execution_id == "exec-fresh"
        assert "exec-fresh" not in runner._active_sessions
