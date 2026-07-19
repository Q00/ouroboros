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

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    BrownfieldContext,
    EvaluationPrinciple,
    ExitCondition,
    InvestmentSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.dependency_analyzer import (
    ACNode,
    DependencyGraph,
    ExecutionStage,
    StagedExecutionPlan,
)
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.level_context import ACContextSummary, LevelContext
from ouroboros.orchestrator.model_routing import ModelRouter, serialize_model_router
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    CheckpointCorruptError,
    CheckpointDispatchMismatchError,
    CheckpointPersistenceError,
    CheckpointPlanMismatchError,
    CheckpointUnreadableError,
    ParallelACExecutor,
)
from ouroboros.orchestrator.profile_loader import load_profile
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


class TestFirstLevelCrashLeavesRecoverableCheckpoint:
    """Round-13 finding #1 (BLOCKING): checkpoints were only written AFTER
    a level completed, so a crash DURING the first level (before any level
    had ever finished) left ZERO durable record of the run — the restart
    minted a fresh execution_id, could not find the original run's durable
    ladder/escalation events (keyed by the ORIGINAL execution_id), and none
    of rounds 9-12's recovery machinery could activate. A minimal
    run-identity checkpoint (same shape as the per-level one, zero
    progress) must now exist from BEFORE the first AC dispatch."""

    @pytest.mark.asyncio
    async def test_crash_during_first_level_leaves_recoverable_identity(
        self, tmp_path: Path
    ) -> None:
        """The review's exact scenario: crash during the FIRST level,
        before any level-completion checkpoint would normally be written.
        A checkpoint must nonetheless exist (execution_id + the
        policy/contract fields), and a subsequent recovery must find and
        restore it instead of starting completely fresh."""
        seed = _seed("First-level crash leaves recoverable identity")
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"

        # --- Run 1: crashes DURING level 1 — no level ever completed.
        run1_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run1_events.initialize()
        run1_ckpt = CheckpointStore(base_path=ckpt_path)
        run1_ckpt.initialize()
        run1 = _executor(
            event_store=run1_events,
            checkpoint_store=run1_ckpt,
            lateral_escalation_enabled=True,
            parked_retry_backoff_seconds=123.0,
        )
        run1._run_batch_with_verify_and_retry = AsyncMock(
            side_effect=RuntimeError("simulated crash during the first level")
        )
        with pytest.raises(BaseException):
            await run1.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=_plan(),
            )
        await run1_events.close()

        # The run-identity checkpoint exists despite zero completed levels,
        # carrying the execution_id and every policy/contract group the
        # recovery block restores.
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        state = saved.value.state
        assert state["checkpoint_contract_version"] == 2
        assert state["execution_id"] == "exec-original"
        assert state["completed_levels"] == 0
        assert state["execution_plan"] == run1._serialize_execution_plan(_plan())
        assert "execution_profile" in state
        assert state["retry_policy"]["lateral_escalation_enabled"] is True
        assert state["retry_policy"]["parked_retry_backoff_seconds"] == 123.0
        assert "reasoning_effort" in state["retry_policy"]
        assert "run_verify_commands" in state["retry_policy"]
        assert "verify_command_timeout_seconds" in state["retry_policy"]
        assert "model_routing" in state
        assert state["execution_semantics"]["cross_harness_redispatch_enabled"] is False
        assert "decomposition_mode" in state["execution_semantics"]
        assert state["owner"]["pid"] == os.getpid()

        # --- Run 2: crash-restart. Recovery must locate the original
        # execution_id and the retry policy the run STARTED with (current
        # config now says escalation is DISABLED — the persisted policy
        # must win, per round 9 #2), then dispatch level 1 from scratch.
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            lateral_escalation_enabled=False,
        )
        captured: dict[str, object] = {}

        async def _recovered_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            captured["execution_id"] = kwargs["execution_id"]
            captured["batch_executable"] = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]

        run2._run_batch_with_verify_and_retry = AsyncMock(side_effect=_recovered_stage_runner)
        result = await run2.execute_parallel(
            seed,
            session_id="session-restart",
            execution_id="exec-restart",
            tools=[],
            system_prompt="system",
            execution_plan=_plan(),
        )

        # Not a completely fresh start: the ORIGINAL execution_id was
        # restored (so ladder/attestation loaders read the original
        # aggregate) and the original run's escalation policy survived the
        # restart's contrary config.
        assert captured["batch_executable"] == [0]
        assert captured["execution_id"] == "exec-original"
        assert result.execution_id == "exec-original"
        assert run2._lateral_escalation_enabled is True
        await run2_events.close()


class _AnchorFailingStore(CheckpointStore):
    """Real store whose first N ``save`` calls fail (transient write blip)."""

    def __init__(self, base_path: Path, *, fail_first_saves: int) -> None:
        super().__init__(base_path=base_path)
        self.remaining_failures = fail_first_saves
        self.save_attempts = 0

    def save(self, checkpoint):  # type: ignore[no-untyped-def]
        self.save_attempts += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            from ouroboros.core.errors import PersistenceError
            from ouroboros.core.types import Result

            return Result.err(
                PersistenceError(
                    "simulated transient checkpoint write failure",
                    operation="write",
                    details={"seed_id": checkpoint.seed_id},
                )
            )
        return super().save(checkpoint)


class TestAnchorSaveFailureBlocksDispatch:
    """A missing run-start anchor must stop the launch before any AC work."""

    @pytest.mark.asyncio
    async def test_failed_anchor_write_refuses_launch_before_dispatch(self, tmp_path: Path) -> None:
        seed = _seed("Anchor save failure blocks dispatch")
        ckpt_path = tmp_path / "checkpoints"
        run1_ckpt = _AnchorFailingStore(ckpt_path, fail_first_saves=1)
        run1_ckpt.initialize()
        run1 = _executor(
            event_store=AsyncMock(),
            checkpoint_store=run1_ckpt,
        )
        run1._run_batch_with_verify_and_retry = AsyncMock()
        with pytest.raises(CheckpointPersistenceError):
            await run1.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=_plan(),
            )
        assert run1_ckpt.save_attempts == 1
        run1._run_batch_with_verify_and_retry.assert_not_awaited()
        assert CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id).is_err

    @pytest.mark.asyncio
    async def test_successful_anchor_allows_dispatch(self, tmp_path: Path) -> None:
        seed = _seed("Successful anchor allows dispatch")
        ckpt_path = tmp_path / "checkpoints"
        store = _AnchorFailingStore(ckpt_path, fail_first_saves=0)
        store.initialize()
        executor = _executor(
            event_store=AsyncMock(),
            checkpoint_store=store,
        )

        async def _complete_level(**kwargs: object) -> list[ACExecutionResult]:
            return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]

        executor._run_batch_with_verify_and_retry = AsyncMock(side_effect=_complete_level)
        result = await executor.execute_parallel(
            seed,
            session_id="session-live",
            execution_id="exec-live",
            tools=[],
            system_prompt="system",
            execution_plan=_plan(),
        )
        assert result.success_count == 1
        executor._run_batch_with_verify_and_retry.assert_awaited_once()
        loaded = store.load(seed.metadata.seed_id)
        assert loaded.is_ok
        assert loaded.value.state["completed_levels"] == 1


class TestDegradedCheckpointReadFailsClosed:
    """Round-15 finding #2 (BLOCKING, load direction): a checkpoint READ
    that cannot confirm whether a checkpoint exists used to fall through to
    "no checkpoint, run fresh" — silently bypassing the ownership gate and
    every restoration round 9-13 added. An indeterminate read must refuse
    the launch loudly BEFORE any AC work; a CONFIRMED absent checkpoint
    stays an ordinary fresh launch."""

    @pytest.mark.asyncio
    async def test_confirmed_absent_checkpoint_returns_none(self, tmp_path: Path) -> None:
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        executor = _executor(event_store=AsyncMock(), checkpoint_store=store)
        assert await executor._load_checkpoint_for_recovery("seed-never-saved") is None

    @pytest.mark.asyncio
    async def test_unreadable_checkpoint_raises_instead_of_running_fresh(
        self, tmp_path: Path
    ) -> None:
        from ouroboros.orchestrator.parallel_executor import CheckpointUnreadableError

        seed = _seed("Degraded checkpoint read fails closed")
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        # A checkpoint FILE exists but is corrupt at every rollback level:
        # the store can neither return it nor confirm absence.
        corrupt = store._get_checkpoint_path(seed.metadata.seed_id)
        corrupt.write_text("{ not json")
        executor = _executor(event_store=AsyncMock(), checkpoint_store=store)
        with pytest.raises(CheckpointUnreadableError):
            await executor._load_checkpoint_for_recovery(seed.metadata.seed_id, max_retries=1)

    @pytest.mark.asyncio
    async def test_launch_refuses_and_dispatches_nothing_on_degraded_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import anyio as anyio_module

        from ouroboros.orchestrator.parallel_executor import CheckpointUnreadableError

        seed = _seed("Degraded read refuses launch")
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        corrupt = store._get_checkpoint_path(seed.metadata.seed_id)
        corrupt.write_text("{ not json")

        async def _instant_sleep(_delay: float) -> None:
            await asyncio.sleep(0)

        monkeypatch.setattr(anyio_module, "sleep", _instant_sleep)
        executor = _executor(event_store=AsyncMock(), checkpoint_store=store)
        executor._run_batch_with_verify_and_retry = AsyncMock()
        with pytest.raises(CheckpointUnreadableError):
            await executor.execute_parallel(
                seed,
                session_id="session-live",
                execution_id="exec-live",
                tools=[],
                system_prompt="system",
                execution_plan=_plan(),
            )
        # Fail-closed means BEFORE any AC work: nothing was dispatched.
        executor._run_batch_with_verify_and_retry.assert_not_called()


def _seed_with_id(seed_id: str, *, goal: str, ac: str) -> Seed:
    return Seed(
        goal=goal,
        constraints=(),
        acceptance_criteria=(ac,),
        ontology_schema=OntologySchema(name="Checkpoint", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05, seed_id=seed_id),
    )


def _plan_for(seed: Seed) -> StagedExecutionPlan:
    return StagedExecutionPlan(
        nodes=tuple(
            ACNode(index=i, content=str(ac)) for i, ac in enumerate(seed.acceptance_criteria)
        ),
        stages=(ExecutionStage(index=0, ac_indices=tuple(range(len(seed.acceptance_criteria)))),),
    )


class TestCheckpointAdoptionValidatesSeedContent:
    """Round-15 finding #1 (BLOCKING): adoption validated nothing but the
    ``seed_id`` KEY — a random uuid naming an object, not its content. A
    Seed whose goal AND acceptance criteria were changed under the same
    seed_id adopted the old content's completed_levels/ac_statuses
    wholesale: recovery dispatched NOTHING and reported SUCCESS while
    describing the CHANGED AC content as "restored" — attributing progress
    to work that never executed. The checkpoint now carries a semantic
    content fingerprint (goal + per-AC ``derive_semantic_ac_key``) that
    must match the currently-supplied Seed before ANY progress is
    adopted."""

    @staticmethod
    async def _complete_run_and_leave_checkpoint(tmp_path: Path, seed: Seed) -> Path:
        """Run seed to full completion, leaving its checkpoint behind (the
        runner-side terminal delete never ran — this is the executor-only
        shape every test in this file uses)."""
        ckpt_path = tmp_path / "checkpoints"
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        run = _executor(event_store=AsyncMock(), checkpoint_store=store)
        run._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[
                ACExecutionResult(
                    ac_index=0, ac_content=str(seed.acceptance_criteria[0]), success=True
                )
            ]
        )
        await run.execute_parallel(
            seed,
            session_id="session-a",
            execution_id="exec-a",
            tools=[],
            system_prompt="system",
            execution_plan=_plan_for(seed),
        )
        assert CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id).is_ok
        return ckpt_path

    @pytest.mark.asyncio
    async def test_changed_seed_content_same_seed_id_is_not_silently_adopted(
        self, tmp_path: Path
    ) -> None:
        """The review's exact probe: checkpoint saved for content A, then
        recovery attempted with DIFFERENT content B under the SAME seed_id.
        B must NOT inherit A's progress: no false "already done" skip, no
        false success, no "[Restored from checkpoint]" attribution of B's
        content to A's work."""
        seed_a = _seed_with_id("seed_shared_id", goal="Ship feature A", ac="Implement feature A")
        ckpt_path = await self._complete_run_and_leave_checkpoint(tmp_path, seed_a)

        seed_b = _seed_with_id(
            "seed_shared_id",
            goal="Ship an entirely different feature B",
            ac="Implement the unrelated feature B",
        )
        db_path = tmp_path / "events.db"
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        dispatched: list[list[int]] = []

        async def _fresh_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            dispatched.append(list(kwargs["batch_executable"]))  # type: ignore[call-overload]
            return [
                ACExecutionResult(
                    ac_index=0, ac_content="Implement the unrelated feature B", success=True
                )
            ]

        run2._run_batch_with_verify_and_retry = AsyncMock(side_effect=_fresh_stage_runner)
        result = await run2.execute_parallel(
            seed_b,
            session_id="session-b",
            execution_id="exec-b",
            tools=[],
            system_prompt="system",
            execution_plan=_plan_for(seed_b),
        )
        await run2_events.close()

        # B's work actually RAN — no silent "already done" skip.
        assert dispatched == [[0]]
        # Nothing was misattributed as restored from A's progress.
        assert all(r.final_message != "[Restored from checkpoint]" for r in result.results)
        # And this is a genuinely FRESH run, not a continuation of A's:
        # the caller's execution_id was kept, not A's restored one.
        assert result.execution_id == "exec-b"
        # The mismatched checkpoint was discarded and re-keyed to B's
        # content: the new durable checkpoint now belongs to exec-b.
        reloaded = CheckpointStore(base_path=ckpt_path).load("seed_shared_id")
        assert reloaded.is_ok
        assert reloaded.value.state["execution_id"] == "exec-b"

    @pytest.mark.asyncio
    async def test_same_seed_content_still_adopts(self, tmp_path: Path) -> None:
        """Control: an identical-content Seed under the same seed_id is a
        genuine resume — adoption must keep working (the round-9 #2
        escalation guarantee depends on it)."""
        seed_a = _seed_with_id("seed_shared_id", goal="Ship feature A", ac="Implement feature A")
        ckpt_path = await self._complete_run_and_leave_checkpoint(tmp_path, seed_a)
        seed_same = _seed_with_id("seed_shared_id", goal="Ship feature A", ac="Implement feature A")
        db_path = tmp_path / "events.db"
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()
        result = await run2.execute_parallel(
            seed_same,
            session_id="session-b",
            execution_id="exec-b",
            tools=[],
            system_prompt="system",
            execution_plan=_plan_for(seed_same),
        )
        await run2_events.close()
        # Adopted: the completed level was skipped and the ORIGINAL run's
        # execution_id restored — the pre-existing genuine-resume behavior.
        run2._run_batch_with_verify_and_retry.assert_not_called()
        assert result.execution_id == "exec-a"
        assert result.results[0].final_message == "[Restored from checkpoint]"

    @pytest.mark.asyncio
    async def test_legacy_checkpoint_without_fingerprint_keeps_adopt_posture(
        self, tmp_path: Path
    ) -> None:
        """One-time migration: a checkpoint written before the fingerprint
        existed must stay resumable (the convention every other restored
        field follows)."""
        from ouroboros.persistence.checkpoint import CheckpointData

        seed_a = _seed_with_id("seed_shared_id", goal="Ship feature A", ac="Implement feature A")
        ckpt_path = await self._complete_run_and_leave_checkpoint(tmp_path, seed_a)
        store = CheckpointStore(base_path=ckpt_path)
        legacy_state = dict(store.load("seed_shared_id").value.state)
        del legacy_state["seed_fingerprint"]
        assert store.save(
            CheckpointData.create(
                seed_id="seed_shared_id", phase="parallel_execution", state=legacy_state
            )
        ).is_ok

        db_path = tmp_path / "events.db"
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()
        result = await run2.execute_parallel(
            seed_a,
            session_id="session-b",
            execution_id="exec-b",
            tools=[],
            system_prompt="system",
            execution_plan=_plan_for(seed_a),
        )
        await run2_events.close()
        run2._run_batch_with_verify_and_retry.assert_not_called()
        assert result.execution_id == "exec-a"

    @pytest.mark.asyncio
    async def test_malformed_fingerprint_blocks_launch_and_is_preserved(
        self, tmp_path: Path
    ) -> None:
        """A present-but-unverifiable fingerprint must refuse adoption
        (fresh run, loudly) — adopting unverifiable progress risks the
        silent-false-success direction."""
        from ouroboros.persistence.checkpoint import CheckpointData

        seed_a = _seed_with_id("seed_shared_id", goal="Ship feature A", ac="Implement feature A")
        ckpt_path = await self._complete_run_and_leave_checkpoint(tmp_path, seed_a)
        store = CheckpointStore(base_path=ckpt_path)
        broken_state = dict(store.load("seed_shared_id").value.state)
        broken_state["seed_fingerprint"] = 12345
        assert store.save(
            CheckpointData.create(
                seed_id="seed_shared_id", phase="parallel_execution", state=broken_state
            )
        ).is_ok

        db_path = tmp_path / "events.db"
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[
                ACExecutionResult(ac_index=0, ac_content="Implement feature A", success=True)
            ]
        )
        with pytest.raises(CheckpointCorruptError):
            await run2.execute_parallel(
                seed_a,
                session_id="session-b",
                execution_id="exec-b",
                tools=[],
                system_prompt="system",
                execution_plan=_plan_for(seed_a),
            )
        await run2_events.close()
        run2._run_batch_with_verify_and_retry.assert_not_called()
        reloaded = CheckpointStore(base_path=ckpt_path).load("seed_shared_id")
        assert reloaded.is_ok
        assert reloaded.value.state["seed_fingerprint"] == 12345


def _rich_seed(**overrides: object) -> Seed:
    """A Seed populating EVERY semantic-surface field the v2 fingerprint
    must cover, under a fixed seed_id so only content distinguishes it."""
    fields: dict[str, object] = {
        "goal": "Ship feature A",
        "task_type": "code",
        "constraints": ("Python 3.12+", "No external database"),
        "acceptance_criteria": (
            AcceptanceCriterionSpec(
                description="Implement feature A",
                investment=InvestmentSpec(difficulty="low", stakes="low"),
            ),
        ),
        "brownfield_context": BrownfieldContext(
            project_type="brownfield",
            existing_patterns=("repository pattern",),
        ),
        "ontology_schema": OntologySchema(name="Checkpoint", description="Test schema"),
        "evaluation_principles": (
            EvaluationPrinciple(name="completeness", description="All requirements met"),
        ),
        "exit_conditions": (
            ExitCondition(name="done", description="All ACs pass", criteria="100% pass"),
        ),
        "metadata": SeedMetadata(ambiguity_score=0.05, seed_id="seed_shared_id"),
        "plugin_handoff": {"queue": "alpha"},
    }
    fields.update(overrides)
    return Seed(**fields)  # type: ignore[arg-type]


class TestSeedFingerprintCoversFullSemanticSurface:
    """Round-16 finding #2 (BLOCKING): the round-15 fingerprint hashed ONLY
    goal + per-AC semantic keys, so a checkpoint saved under one set of
    ``constraints``/``task_type``/``brownfield_context``/``ontology_schema``/
    evaluation-exit contracts/plugin fields/per-AC ``investment`` values was
    silently adopted by a resume where those values had materially changed
    (several change prompts or routing) — old progress skipped work under
    semantics that no longer match what was about to execute. The v2
    fingerprint must diverge for each of those fields, stay identical for a
    genuine resume, and keep verifying legacy v1 checkpoints against the v1
    scheme (one-time migration posture)."""

    @pytest.mark.parametrize(
        ("field", "changed"),
        [
            ("constraints", ("Rust only",)),
            ("task_type", "research"),
            (
                "brownfield_context",
                BrownfieldContext(project_type="greenfield"),
            ),
            (
                "ontology_schema",
                OntologySchema(name="Checkpoint", description="A different conceptual lens"),
            ),
            (
                "evaluation_principles",
                (EvaluationPrinciple(name="completeness", description="All met", weight=0.2),),
            ),
            (
                "exit_conditions",
                (ExitCondition(name="done", description="Different exit", criteria="new bar"),),
            ),
            (
                "acceptance_criteria",
                (
                    AcceptanceCriterionSpec(
                        description="Implement feature A",
                        investment=InvestmentSpec(difficulty="high", stakes="high"),
                    ),
                ),
            ),
            ("plugin_handoff", {"queue": "beta"}),
        ],
    )
    def test_material_change_diverges_fingerprint(self, field: str, changed: object) -> None:
        """Each of the review's cited fields must invalidate the checkpoint
        when it changes under the same seed_id."""
        base = ParallelACExecutor._seed_semantic_fingerprint(_rich_seed())
        mutated = ParallelACExecutor._seed_semantic_fingerprint(_rich_seed(**{field: changed}))
        assert base != mutated, f"fingerprint ignored a material change to {field!r}"

    def test_identical_content_matches_and_volatile_metadata_is_excluded(self) -> None:
        """A genuine resume re-supplies identical content — possibly under
        regenerated volatile metadata — and must keep matching."""
        base = ParallelACExecutor._seed_semantic_fingerprint(_rich_seed())
        assert base == ParallelACExecutor._seed_semantic_fingerprint(_rich_seed())
        remetadata = _rich_seed(
            metadata=SeedMetadata(
                ambiguity_score=0.19,
                seed_id="seed_other_id",
                interview_id="interview-42",
            )
        )
        assert base == ParallelACExecutor._seed_semantic_fingerprint(remetadata)

    @pytest.mark.asyncio
    async def test_changed_constraints_same_seed_id_is_not_silently_adopted(
        self, tmp_path: Path
    ) -> None:
        """End to end: checkpoint saved under constraints A, resume with
        materially different constraints under the SAME seed_id and the
        SAME goal/AC text — the pre-fix fingerprint matched and adopted the
        stale progress wholesale; now the checkpoint must be discarded and
        the work actually dispatched."""
        seed_a = _rich_seed()
        ckpt_path = tmp_path / "checkpoints"
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        run1 = _executor(event_store=AsyncMock(), checkpoint_store=store)
        run1._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[
                ACExecutionResult(ac_index=0, ac_content="Implement feature A", success=True)
            ]
        )
        await run1.execute_parallel(
            seed_a,
            session_id="session-a",
            execution_id="exec-a",
            tools=[],
            system_prompt="system",
            execution_plan=_plan_for(seed_a),
        )
        assert CheckpointStore(base_path=ckpt_path).load("seed_shared_id").is_ok

        seed_b = _rich_seed(constraints=("Rust only",))
        db_path = tmp_path / "events.db"
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        dispatched: list[list[int]] = []

        async def _fresh_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            dispatched.append(list(kwargs["batch_executable"]))  # type: ignore[call-overload]
            return [ACExecutionResult(ac_index=0, ac_content="Implement feature A", success=True)]

        run2._run_batch_with_verify_and_retry = AsyncMock(side_effect=_fresh_stage_runner)
        result = await run2.execute_parallel(
            seed_b,
            session_id="session-b",
            execution_id="exec-b",
            tools=[],
            system_prompt="system",
            execution_plan=_plan_for(seed_b),
        )
        await run2_events.close()

        # The changed-constraints run actually dispatched — no silent
        # "already done" skip off the other constraint set's progress.
        assert dispatched == [[0]]
        assert all(r.final_message != "[Restored from checkpoint]" for r in result.results)
        assert result.execution_id == "exec-b"

    @pytest.mark.asyncio
    async def test_v1_fingerprint_checkpoint_still_adopts_matching_content(
        self, tmp_path: Path
    ) -> None:
        """One-time migration: a checkpoint saved under the round-15 v1
        scheme must stay resumable when its v1 recomputation matches — the
        same posture legacy fingerprint-less checkpoints keep."""
        from ouroboros.persistence.checkpoint import CheckpointData

        seed = _rich_seed()
        ckpt_path = tmp_path / "checkpoints"
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        run1 = _executor(event_store=AsyncMock(), checkpoint_store=store)
        run1._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[
                ACExecutionResult(ac_index=0, ac_content="Implement feature A", success=True)
            ]
        )
        await run1.execute_parallel(
            seed,
            session_id="session-a",
            execution_id="exec-a",
            tools=[],
            system_prompt="system",
            execution_plan=_plan_for(seed),
        )
        v1_state = dict(store.load("seed_shared_id").value.state)
        assert str(v1_state["seed_fingerprint"]).startswith("v2:")
        v1_state["seed_fingerprint"] = ParallelACExecutor._seed_semantic_fingerprint_v1(seed)
        assert store.save(
            CheckpointData.create(
                seed_id="seed_shared_id", phase="parallel_execution", state=v1_state
            )
        ).is_ok

        db_path = tmp_path / "events.db"
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()
        result = await run2.execute_parallel(
            seed,
            session_id="session-b",
            execution_id="exec-b",
            tools=[],
            system_prompt="system",
            execution_plan=_plan_for(seed),
        )
        await run2_events.close()
        run2._run_batch_with_verify_and_retry.assert_not_called()
        assert result.execution_id == "exec-a"


class TestMalformedCheckpointProgressFailsClosed:
    """Round-16 finding #3 (BLOCKING): recovery used to apply checkpoint
    progress fields to local execution state INCREMENTALLY — a hash-valid
    checkpoint with a type-mangled field (the review's probe:
    ``completed_levels="1"``, a string) partially restored the original
    execution_id before a later conversion raised, leaving recovery torn
    between two runs' identities. Every progress field must now be
    validated atomically BEFORE any of it is applied; a malformed
    checkpoint takes the established fail-closed path — discarded as
    stale, all levels run fresh under the CALLER's identity, loud operator
    warning — never a crash, never a partial application."""

    @staticmethod
    def _two_ac_seed() -> Seed:
        return Seed(
            goal="Malformed checkpoint progress",
            constraints=(),
            acceptance_criteria=("Parent work", "Verify integration"),
            ontology_schema=OntologySchema(name="Recovery", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

    @staticmethod
    def _two_stage_plan() -> StagedExecutionPlan:
        return StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="Parent work"),
                ACNode(index=1, content="Verify integration", depends_on=(0,)),
            ),
            stages=(
                ExecutionStage(index=0, ac_indices=(0,)),
                ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
            ),
        )

    async def _crash_after_level_one(self, ckpt_path: Path, seed: Seed) -> None:
        """Run 1: level 1 completes (checkpoint saved under exec-original),
        then the process dies during level 2 — a genuine crash shape whose
        checkpoint would ordinarily be adopted."""
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        original = _executor(event_store=AsyncMock(), checkpoint_store=store)

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            raise RuntimeError("simulated process crash during level 2")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        with pytest.raises(BaseException, match="simulated process crash|unhandled errors"):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=self._two_stage_plan(),
            )
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["execution_id"] == "exec-original"
        assert saved.value.state["completed_levels"] == 1

    @pytest.mark.parametrize(
        "corruption",
        [
            # The review's exact probe: a string where an int must be.
            {"completed_levels": "1"},
            # A non-integer AC index key AFTER valid entries: the pre-fix
            # incremental apply raised mid-loop with execution_id already
            # reassigned (the torn shape this class guards against).
            {"ac_statuses": {"0": "completed", "not-an-int": "completed", "1": "pending"}},
            # An unknown status value is corruption, not progress.
            {"ac_statuses": {"0": "totally-bogus", "1": "pending"}},
            # A non-integer failed index.
            {"failed_indices": ["one"]},
            # A type-mangled completed count.
            {"completed_count": "2"},
            # Nested context elements must be typed mappings, not merely be
            # wrapped by a list that passes the outer shape check.
            {"level_contexts": [42]},
            {
                "level_contexts": [
                    {
                        "level_number": 1,
                        "completed_acs": [42],
                        "coordinator_review": None,
                    }
                ]
            },
            # AC indices are normalized before uniqueness/range checks.
            {"ac_statuses": {"0": "completed", "00": "completed", "1": "pending"}},
            {"ac_statuses": {"0": "completed", "2": "pending"}},
            {"failed_indices": [0, "0"]},
            # Relational corruption across otherwise well-typed fields.
            {"failed_indices": [0]},
            {"completed_count": 0},
            {"completed_levels": 3},
            {"plan_total_stages": 3},
            {"level_contexts": []},
        ],
        ids=[
            "string_completed_levels",
            "non_integer_ac_status_key",
            "unknown_ac_status_value",
            "non_integer_failed_index",
            "string_completed_count",
            "non_mapping_level_context",
            "non_mapping_completed_ac_context",
            "duplicate_normalized_ac_status_key",
            "out_of_range_ac_status_key",
            "duplicate_failed_index",
            "failed_indices_status_mismatch",
            "completed_count_status_mismatch",
            "completed_levels_exceeds_plan",
            "plan_stage_count_mismatch",
            "completed_context_missing",
        ],
    )
    @pytest.mark.asyncio
    async def test_malformed_progress_blocks_launch_and_preserves_checkpoint(
        self, tmp_path: Path, corruption: dict[str, object]
    ) -> None:
        from ouroboros.persistence.checkpoint import CheckpointData

        seed = self._two_ac_seed()
        ckpt_path = tmp_path / "checkpoints"
        await self._crash_after_level_one(ckpt_path, seed)

        store = CheckpointStore(base_path=ckpt_path)
        broken_state = dict(store.load(seed.metadata.seed_id).value.state)
        broken_state.update(corruption)
        assert store.save(
            CheckpointData.create(
                seed_id=seed.metadata.seed_id,
                phase="parallel_execution",
                state=broken_state,
            )
        ).is_ok

        db_path = tmp_path / "events.db"
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()
        with pytest.raises(CheckpointCorruptError):
            await run2.execute_parallel(
                seed,
                session_id="session-restarted",
                execution_id="exec-restarted",
                tools=[],
                system_prompt="system",
                execution_plan=self._two_stage_plan(),
            )
        await run2_events.close()
        run2._run_batch_with_verify_and_retry.assert_not_awaited()
        reloaded = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert reloaded.is_ok
        assert reloaded.value.state == broken_state

    @pytest.mark.asyncio
    async def test_legacy_missing_completed_context_blocks_downstream_resume(
        self, tmp_path: Path
    ) -> None:
        """Absent legacy context cannot silently resume dependent work."""
        from ouroboros.persistence.checkpoint import CheckpointData

        seed = self._two_ac_seed()
        ckpt_path = tmp_path / "checkpoints"
        await self._crash_after_level_one(ckpt_path, seed)

        store = CheckpointStore(base_path=ckpt_path)
        saved = store.load(seed.metadata.seed_id)
        assert saved.is_ok
        legacy_state = dict(saved.value.state)
        legacy_state.pop("level_contexts")
        assert store.save(
            CheckpointData.create(
                seed_id=seed.metadata.seed_id,
                phase="parallel_execution",
                state=legacy_state,
            )
        ).is_ok

        events = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
        await events.initialize()
        resumed = _executor(
            event_store=events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        resumed._run_batch_with_verify_and_retry = AsyncMock()

        with pytest.raises(CheckpointUnreadableError, match="lacks the AC context"):
            await resumed.execute_parallel(
                seed,
                session_id="session-restarted",
                execution_id="exec-restarted",
                tools=[],
                system_prompt="system",
                execution_plan=self._two_stage_plan(),
            )

        await events.close()
        resumed._run_batch_with_verify_and_retry.assert_not_awaited()

    def test_progress_validation_accepts_the_saved_shape(self, tmp_path: Path) -> None:
        """Control: the exact state a real save writes must validate clean
        (a genuine resume keeps working), including absent legacy fields."""
        plan = self._two_stage_plan()
        state = {
            "execution_id": "exec-original",
            "completed_levels": 1,
            "plan_total_stages": 2,
            "execution_plan": ParallelACExecutor._serialize_execution_plan(plan),
            "ac_statuses": {"0": "completed", "1": "pending"},
            "failed_indices": [],
            "satisfied_externally_indices": [],
            "completed_count": 1,
            "level_contexts": [
                {
                    "level_number": 1,
                    "completed_acs": [
                        {
                            "ac_index": 0,
                            "ac_content": "Parent work",
                            "success": True,
                            "tools_used": [],
                            "files_modified": [],
                            "key_output": "done",
                            "public_api": "",
                        }
                    ],
                    "coordinator_review": None,
                }
            ],
        }
        cp = SimpleNamespace(state=state)
        assert ParallelACExecutor._checkpoint_progress_malformed(cp, total_acs=2) is None
        # Legacy shape: progress keys absent entirely — adopt posture kept.
        assert ParallelACExecutor._checkpoint_progress_malformed(SimpleNamespace(state={})) is None
        # Integer keys (in-process relaunch, no JSON round-trip) are valid.
        cp_int_keys = SimpleNamespace(state={"ac_statuses": {0: "completed"}})
        assert ParallelACExecutor._checkpoint_progress_malformed(cp_int_keys) is None

    def test_progress_validation_accepts_external_completion_without_context(self) -> None:
        plan = self._two_stage_plan()
        state = {
            "completed_levels": 1,
            "plan_total_stages": 2,
            "execution_plan": ParallelACExecutor._serialize_execution_plan(plan),
            "dispatch_contract": {
                "externally_satisfied_ac_indices": [0],
                "reconciled_level_contexts": [],
            },
            "ac_statuses": {"0": "completed", "1": "pending"},
            "failed_indices": [],
            "satisfied_externally_indices": [0],
            "completed_count": 1,
            "level_contexts": [],
        }

        assert (
            ParallelACExecutor._checkpoint_progress_malformed(
                SimpleNamespace(state=state), total_acs=2
            )
            is None
        )

    def test_progress_validation_accepts_reconciled_context_for_pending_ac(self) -> None:
        plan = self._two_stage_plan()
        reconciled_context = {
            "level_number": 1,
            "completed_acs": [
                {
                    "ac_index": 0,
                    "ac_content": "Parent work from prior attempt",
                    "success": True,
                    "tools_used": ["Edit"],
                    "files_modified": ["shared.py"],
                    "key_output": "workspace reconciled",
                    "public_api": "",
                }
            ],
            "coordinator_review": None,
        }
        state = {
            "completed_levels": 0,
            "plan_total_stages": 2,
            "execution_plan": ParallelACExecutor._serialize_execution_plan(plan),
            "dispatch_contract": {
                "externally_satisfied_ac_indices": [],
                "reconciled_level_contexts": [reconciled_context],
            },
            "ac_statuses": {"0": "pending", "1": "pending"},
            "failed_indices": [],
            "completed_count": 0,
            "level_contexts": [reconciled_context],
        }

        assert (
            ParallelACExecutor._checkpoint_progress_malformed(
                SimpleNamespace(state=state), total_acs=2
            )
            is None
        )

    @pytest.mark.parametrize(
        ("state", "expected_fragment"),
        [
            ({"completed_levels": "1"}, "completed_levels"),
            ({"completed_levels": True}, "completed_levels"),
            ({"completed_levels": -1}, "completed_levels"),
            ({"execution_id": 42}, "execution_id"),
            ({"ac_statuses": ["completed"]}, "ac_statuses"),
            ({"ac_statuses": {"x": "completed"}}, "ac_statuses"),
            ({"ac_statuses": {"0": 7}}, "ac_statuses"),
            ({"failed_indices": "0,1"}, "failed_indices"),
            ({"failed_indices": [1.5]}, "failed_indices"),
            ({"completed_count": None}, "completed_count"),
            ({"level_contexts": "corrupt"}, "level_contexts"),
            ({"level_contexts": [42]}, "level_contexts"),
        ],
    )
    def test_progress_validation_rejects_each_malformed_field(
        self, state: dict[str, object], expected_fragment: str
    ) -> None:
        detail = ParallelACExecutor._checkpoint_progress_malformed(SimpleNamespace(state=state))
        assert detail is not None
        assert expected_fragment in detail

    @pytest.mark.parametrize(
        ("state_update", "expected_fragment"),
        [
            (
                {"ac_statuses": {"0": "completed", "00": "completed", "1": "pending"}},
                "duplicate normalized",
            ),
            ({"ac_statuses": {"0": "completed", "2": "pending"}}, "Seed AC range"),
            ({"failed_indices": [0, "0"]}, "duplicate AC index"),
            ({"failed_indices": [0]}, "does not match failed"),
            ({"completed_count": 0}, "does not match completed"),
            ({"completed_levels": 3}, "exceeds plan_total_stages"),
            ({"plan_total_stages": 3}, "does not match execution_plan.stages"),
            ({"level_contexts": []}, "do not match completed"),
            (
                {
                    "level_contexts": [
                        {
                            "level_number": 2,
                            "completed_acs": [
                                {
                                    "ac_index": 0,
                                    "ac_content": "Parent work",
                                    "success": True,
                                    "tools_used": [],
                                    "files_modified": [],
                                    "key_output": "done",
                                    "public_api": "",
                                }
                            ],
                            "coordinator_review": None,
                        }
                    ]
                },
                "wrong execution plan stage",
            ),
        ],
    )
    def test_progress_validation_rejects_relational_corruption(
        self,
        state_update: dict[str, object],
        expected_fragment: str,
    ) -> None:
        state: dict[str, object] = {
            "completed_levels": 1,
            "plan_total_stages": 2,
            "execution_plan": ParallelACExecutor._serialize_execution_plan(self._two_stage_plan()),
            "ac_statuses": {"0": "completed", "1": "pending"},
            "failed_indices": [],
            "completed_count": 1,
            "level_contexts": [
                {
                    "level_number": 1,
                    "completed_acs": [
                        {
                            "ac_index": 0,
                            "ac_content": "Parent work",
                            "success": True,
                            "tools_used": [],
                            "files_modified": [],
                            "key_output": "done",
                            "public_api": "",
                        }
                    ],
                    "coordinator_review": None,
                }
            ],
        }
        state.update(state_update)
        detail = ParallelACExecutor._checkpoint_progress_malformed(
            SimpleNamespace(state=state),
            total_acs=2,
        )
        assert detail is not None
        assert expected_fragment in detail


class TestMalformedCheckpointSemanticsFailsClosed:
    """Round-17 finding #3 (BLOCKING): the three execution-semantic restore
    helpers (retry policy, model router, execution semantics) each validate
    their own payload atomically, but a malformed payload used to be
    swallowed INSIDE the helper — log, force escalation gates open, return
    — without signalling the RC3 recovery caller, which adopted the
    checkpoint anyway. The recovered run then executed under a torn
    MIXTURE: the groups that validated ran with the ORIGINAL run's
    semantics while the malformed group silently ran with the CURRENT
    process's. Semantic payloads must now be validated atomically BEFORE
    any restoration mutates executor state; any malformed group takes the
    SAME fail-closed path malformed progress takes (round-16 #3): the
    whole checkpoint is discarded as corrupt, every level runs fresh under
    the caller's identity and the current process's semantics — everything
    restores together or nothing does."""

    _seed_factory = staticmethod(TestMalformedCheckpointProgressFailsClosed._two_ac_seed)
    _plan_factory = staticmethod(TestMalformedCheckpointProgressFailsClosed._two_stage_plan)

    @staticmethod
    def _valid_semantics_state() -> dict[str, object]:
        return {
            "execution_profile": None,
            "retry_policy": {
                "lateral_escalation_enabled": True,
                "parked_retry_backoff_seconds": 300.0,
                "ac_retry_attempts": 2,
                "reasoning_effort": None,
                "run_verify_commands": True,
                "verify_command_timeout_seconds": 300,
            },
            "model_routing": serialize_model_router(None),
            "execution_semantics": {
                "decomposition_mode": "preflight",
                "max_decomposition_depth": 2,
                "fat_harness_mode": False,
                "cross_harness_redispatch_enabled": False,
                "shadow_replay_enabled": False,
                "context_pack_enabled": None,
                "max_concurrent": 3,
            },
            "prompt_guidance": None,
        }

    async def _crash_after_level_one(self, ckpt_path: Path, seed: Seed) -> None:
        """Run 1 (distinct semantics: backoff 777, bounce_only, fan-out 3):
        level 1 completes and its anchor checkpoint persists, then the
        process dies during level 2 — a genuine crash shape whose
        checkpoint would ordinarily be adopted wholesale."""
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        original = _executor(
            event_store=AsyncMock(),
            checkpoint_store=store,
            parked_retry_backoff_seconds=777.0,
            decomposition_mode="bounce_only",
            max_concurrent=3,
        )

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            raise RuntimeError("simulated process crash during level 2")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        with pytest.raises(BaseException, match="simulated process crash|unhandled errors"):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=self._plan_factory(),
            )
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["execution_id"] == "exec-original"
        assert saved.value.state["retry_policy"]["parked_retry_backoff_seconds"] == 777.0
        assert saved.value.state["execution_semantics"]["decomposition_mode"] == "bounce_only"

    @pytest.mark.parametrize(
        ("field", "corruption"),
        [
            # The review's exact probe: an execution_semantics payload whose
            # OTHER fields are all well-formed, with ONE type-mangled field.
            ("execution_semantics", {"max_concurrent": "3"}),
            ("execution_semantics", {"decomposition_mode": "sideways"}),
            # The same cross-group tear in the other directions: a corrupt
            # retry policy (or router contract) next to well-formed
            # execution semantics.
            ("retry_policy", {"parked_retry_backoff_seconds": "fast"}),
            ("model_routing", {"version": 99, "enabled": True}),
        ],
        ids=[
            "string_max_concurrent",
            "unknown_decomposition_mode",
            "string_backoff",
            "unrecognized_router_version",
        ],
    )
    @pytest.mark.asyncio
    async def test_malformed_semantics_block_launch_and_preserve_checkpoint(
        self, tmp_path: Path, field: str, corruption: dict[str, object]
    ) -> None:
        from ouroboros.persistence.checkpoint import CheckpointData

        seed = self._seed_factory()
        ckpt_path = tmp_path / "checkpoints"
        await self._crash_after_level_one(ckpt_path, seed)

        store = CheckpointStore(base_path=ckpt_path)
        broken_state = dict(store.load(seed.metadata.seed_id).value.state)
        if field == "model_routing":
            # The router contract is opaque/versioned — corrupt it wholesale.
            broken_state[field] = corruption
        else:
            existing = broken_state[field]
            assert isinstance(existing, dict)
            broken_state[field] = {**existing, **corruption}
        assert store.save(
            CheckpointData.create(
                seed_id=seed.metadata.seed_id,
                phase="parallel_execution",
                state=broken_state,
            )
        ).is_ok

        # Restart under DIFFERENT construction semantics, so any restored
        # group is observable.
        db_path = tmp_path / "events.db"
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            parked_retry_backoff_seconds=300.0,
            decomposition_mode="preflight",
            max_concurrent=2,
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()
        with pytest.raises(CheckpointCorruptError):
            await run2.execute_parallel(
                seed,
                session_id="session-restarted",
                execution_id="exec-restarted",
                tools=[],
                system_prompt="system",
                execution_plan=self._plan_factory(),
            )
        await run2_events.close()
        run2._run_batch_with_verify_and_retry.assert_not_awaited()
        assert run2._parked_retry_backoff_seconds == 300.0
        assert run2._decomposition_mode == "preflight"
        assert run2._max_concurrent == 2
        reloaded = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert reloaded.is_ok
        assert reloaded.value.state == broken_state

    @pytest.mark.asyncio
    async def test_unversioned_checkpoint_is_rejected_without_dispatch(
        self, tmp_path: Path
    ) -> None:
        from ouroboros.persistence.checkpoint import CheckpointData

        seed = self._seed_factory()
        ckpt_path = tmp_path / "checkpoints"
        await self._crash_after_level_one(ckpt_path, seed)
        store = CheckpointStore(base_path=ckpt_path)
        saved = store.load(seed.metadata.seed_id)
        assert saved.is_ok
        unversioned_state = dict(saved.value.state)
        unversioned_state.pop("checkpoint_contract_version")
        assert store.save(
            CheckpointData.create(
                seed_id=seed.metadata.seed_id,
                phase="parallel_execution",
                state=unversioned_state,
            )
        ).is_ok

        resumed = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        resumed._run_batch_with_verify_and_retry = AsyncMock()

        with pytest.raises(CheckpointCorruptError, match="format is unsupported"):
            await resumed.execute_parallel(
                seed,
                session_id="session-restarted",
                execution_id="exec-restarted",
                tools=[],
                system_prompt="system",
                execution_plan=self._plan_factory(),
            )

        resumed._run_batch_with_verify_and_retry.assert_not_awaited()

    def test_semantics_validation_accepts_the_saved_shape(self, tmp_path: Path) -> None:
        """Control: the exact semantic payloads a real save writes must
        validate clean; missing groups are indeterminate and rejected."""
        state = self._valid_semantics_state()
        cp = SimpleNamespace(state=state)
        assert ParallelACExecutor._checkpoint_semantics_malformed(cp) is None
        assert "missing" in (
            ParallelACExecutor._checkpoint_semantics_malformed(SimpleNamespace(state={})) or ""
        )

    @pytest.mark.parametrize(
        ("state", "expected_fragment"),
        [
            ({"retry_policy": "corrupt"}, "retry_policy"),
            ({"retry_policy": {"lateral_escalation_enabled": "yes"}}, "lateral_escalation_enabled"),
            (
                {
                    "retry_policy": {
                        "lateral_escalation_enabled": True,
                        "parked_retry_backoff_seconds": float("inf"),
                        "ac_retry_attempts": 0,
                    }
                },
                "parked_retry_backoff_seconds",
            ),
            (
                {
                    "retry_policy": {
                        "lateral_escalation_enabled": True,
                        "parked_retry_backoff_seconds": 300.0,
                        "ac_retry_attempts": -1,
                    }
                },
                "ac_retry_attempts",
            ),
            (
                {
                    "retry_policy": {
                        "lateral_escalation_enabled": True,
                        "parked_retry_backoff_seconds": 300.0,
                        "ac_retry_attempts": 0,
                        "reasoning_effort": "ultra",
                    }
                },
                "reasoning_effort",
            ),
            ({"model_routing": {"version": 99, "enabled": True}}, "model_routing"),
            ({"execution_profile": {"profile": "code"}}, "execution_profile"),
            ({"execution_semantics": "corrupt"}, "execution_semantics"),
            ({"execution_semantics": {"decomposition_mode": "sideways"}}, "decomposition_mode"),
            ({"execution_semantics": {"max_decomposition_depth": True}}, "max_decomposition_depth"),
            ({"execution_semantics": {"fat_harness_mode": 1}}, "fat_harness_mode"),
            ({"execution_semantics": {"context_pack_enabled": "on"}}, "context_pack_enabled"),
            ({"execution_semantics": {"max_concurrent": 0}}, "max_concurrent"),
        ],
    )
    def test_semantics_validation_rejects_each_malformed_field(
        self, state: dict[str, object], expected_fragment: str
    ) -> None:
        complete_state = self._valid_semantics_state()
        complete_state.update(state)
        detail = ParallelACExecutor._checkpoint_semantics_malformed(
            SimpleNamespace(state=complete_state)
        )
        assert detail is not None
        assert expected_fragment in detail

    @pytest.mark.parametrize(
        "missing_group",
        [
            "retry_policy",
            "model_routing",
            "execution_semantics",
            "execution_profile",
            "prompt_guidance",
        ],
    )
    def test_semantics_validation_rejects_missing_versioned_group(self, missing_group: str) -> None:
        state = self._valid_semantics_state()
        state.pop(missing_group)
        detail = ParallelACExecutor._checkpoint_semantics_malformed(SimpleNamespace(state=state))
        assert detail is not None
        assert missing_group in detail


class TestIndeterminateReplayWithCompletedCheckpoint:
    """Round-13 finding #2 (BLOCKING): the terminal-staleness gate's
    indeterminate-replay tiebreaker ("a surviving checkpoint is itself
    evidence of interruption") is wrong exactly when the runner's
    best-effort terminal delete failed: a COMPLETED run's checkpoint
    survives, the degraded event store cannot confirm the terminal record,
    and the old ``return False`` adopted the stale checkpoint — skipping
    every level and reporting SUCCESS with zero AC dispatches. The
    checkpoint's OWN state (``completed_levels == total_levels``) is a
    replay-independent second signal for that shape; a partial checkpoint
    keeps the round-9-protecting adopt posture."""

    @pytest.mark.asyncio
    async def test_completed_checkpoint_failed_delete_indeterminate_replay_reruns(
        self, tmp_path: Path
    ) -> None:
        """The review's exact reproduction: run 1 genuinely COMPLETED (its
        terminal event landed) but its checkpoint delete failed (the
        checkpoint is still present — the executor harness never deletes,
        exactly the runner-delete-failure shape). Run 2's replay of the
        execution aggregate is degraded (every attempt raises →
        indeterminate). Run 2 must NOT silently adopt the full-completion
        checkpoint and report success without dispatching: it must re-run
        the ACs from scratch and warn the operator loudly."""
        seed = _seed("Completed run, failed delete, degraded replay")
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"

        # --- Run 1: completes normally; its final checkpoint marks the
        # whole plan done and the terminal event lands durably.
        from ouroboros.orchestrator.events import create_execution_terminal_event

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
        await run1_events.append(
            create_execution_terminal_event(
                execution_id="exec-original",
                session_id="session-original",
                status="completed",
            )
        )
        await run1_events.close()
        # The runner's terminal delete FAILED: the checkpoint survives and
        # claims full completion.
        leftover = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert leftover.is_ok
        assert leftover.value.state["completed_levels"] == 1

        # --- Run 2: fresh run of the same seed against a DEGRADED event
        # store — every replay attempt raises, so the terminal-record
        # check is indeterminate.
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()

        async def _degraded_replay(*_args: object, **_kwargs: object) -> list[object]:
            raise RuntimeError("simulated degraded event store read")

        run2_events.replay = _degraded_replay  # type: ignore[method-assign]
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

        # The ACs actually ran — never skipped off the ambiguous
        # full-completion checkpoint — under the NEW run's execution_id.
        assert captured["batch_executable"] == [0]
        assert captured["execution_id"] == "exec-rerun"
        assert result2.all_succeeded
        assert result2.execution_id == "exec-rerun"
        assert all(r.final_message != "[Restored from checkpoint]" for r in result2.results)
        # And the ambiguity was surfaced loudly to the operator, never
        # swallowed silently.
        printed = " ".join(
            str(call)
            for call in run2._console.print.call_args_list  # type: ignore[union-attr]
        )
        assert "durable state is uncertain" in printed
        await run2_events.close()

    @pytest.mark.asyncio
    async def test_genuine_crash_with_indeterminate_outcome_replay_blocks_dispatch(
        self, tmp_path: Path
    ) -> None:
        """Unknown finalized outcomes cannot safely be treated as pending."""
        seed = Seed(
            goal="Crashed run resumes under degraded replay",
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

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            raise RuntimeError("simulated crash during level 2")

        run1._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        with pytest.raises(BaseException):
            await run1.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=plan,
            )
        # A genuine crash: NO terminal event was ever recorded.
        await run1_events.close()
        partial = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert partial.is_ok
        assert partial.value.state["completed_levels"] == 1

        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()

        async def _degraded_replay(*_args: object, **_kwargs: object) -> list[object]:
            raise RuntimeError("simulated degraded event store read")

        run2_events.replay = _degraded_replay  # type: ignore[method-assign]
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()
        with pytest.raises(CheckpointUnreadableError):
            await run2.execute_parallel(
                seed,
                session_id="session-resumed",
                execution_id="exec-resumed",
                tools=[],
                system_prompt="system",
                execution_plan=plan,
            )
        run2._run_batch_with_verify_and_retry.assert_not_awaited()
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

    @pytest.mark.asyncio
    async def test_transient_delete_failure_is_retried_until_removed(self, tmp_path: Path) -> None:
        """Round-13 finding #2: the terminal delete is correctness-bearing
        (a leftover COMPLETED checkpoint is the raw material of the
        adopt-and-skip-everything false success), so a transient failure
        must be retried instead of taking one silent best-effort shot."""
        from ouroboros.orchestrator.parallel_executor import ParallelExecutionResult

        seed = _seed("Transient delete failure gets retried")
        runner, store, _ = self._runner_harness(tmp_path)
        self._seed_checkpoint(store, seed)

        real_delete = store.delete
        attempts: list[int] = []

        def _flaky_delete(seed_id: str) -> object:
            attempts.append(1)
            if len(attempts) < 3:
                return SimpleNamespace(is_ok=False, error="simulated transient disk failure")
            return real_delete(seed_id)

        store.delete = _flaky_delete  # type: ignore[method-assign]
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
        # Two transient failures, then the retry succeeded — the stale
        # checkpoint is gone instead of lying in wait for a fresh run.
        assert len(attempts) == 3
        assert store.load(seed.metadata.seed_id).is_err


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
            # Round-14 finding #3: ``None`` — this direct-constructed
            # executor was never given the runner's pinned flag.
            "context_pack_enabled": None,
            # Round-15 finding #5: the shared-workspace concurrency the run
            # dispatched under (this executor's construction default).
            "max_concurrent": 3,
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
            # Round-15 finding #5: the restart's construction concurrency
            # differs from the crashed run's — recovery must restore the
            # original.
            max_concurrent=1,
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
        # Round-15 finding #5: the ORIGINAL run's shared-workspace
        # concurrency was restored over the restart's construction value,
        # and the live semaphore was rebuilt to match it.
        assert recovered._max_concurrent == 3
        assert recovered._semaphore.value == 3
        await recovered_events.close()

    def test_checkpointed_concurrency_restored_and_semaphore_rebuilt(self) -> None:
        """Round-15 finding #5 (BLOCKING): concurrency over the shared
        workspace is execution semantics (sequential sibling effects vs
        interleaved mid-flight writes), so a crash-restart must adopt the
        checkpointed run's ``max_concurrent`` — and rebuild the dispatch
        semaphore, which ``__init__`` sized from the CURRENT process's
        construction value."""
        executor = self._unit_executor(max_concurrent=2)
        executor._restore_checkpoint_execution_semantics(
            {
                "decomposition_mode": "preflight",
                "max_decomposition_depth": 2,
                "fat_harness_mode": False,
                "cross_harness_redispatch_enabled": False,
                "shadow_replay_enabled": False,
                "context_pack_enabled": None,
                "max_concurrent": 5,
            }
        )
        assert executor._max_concurrent == 5
        assert executor._semaphore.value == 5

    def test_legacy_semantics_without_concurrency_keep_current_value(self) -> None:
        """One-time migration: a checkpoint predating the field keeps the
        current construction value (and the original semaphore)."""
        executor = self._unit_executor(max_concurrent=2)
        original_semaphore = executor._semaphore
        executor._restore_checkpoint_execution_semantics(
            {
                "decomposition_mode": "preflight",
                "max_decomposition_depth": 2,
                "fat_harness_mode": False,
                "cross_harness_redispatch_enabled": False,
                "shadow_replay_enabled": False,
                "context_pack_enabled": None,
            }
        )
        assert executor._max_concurrent == 2
        assert executor._semaphore is original_semaphore

    @pytest.mark.parametrize("corrupt_workers", ["3", 0, -1, True, 2.5, None])
    def test_malformed_concurrency_fails_closed(self, corrupt_workers: object) -> None:
        """Present-but-malformed takes the whole mapping down the
        established fail-closed branch: nothing adopted, escalation gate
        forced open."""
        executor = self._unit_executor(max_concurrent=2)
        executor._restore_checkpoint_execution_semantics(
            {
                "decomposition_mode": "preflight",
                "max_decomposition_depth": 2,
                "fat_harness_mode": False,
                "cross_harness_redispatch_enabled": False,
                "shadow_replay_enabled": False,
                "context_pack_enabled": None,
                "max_concurrent": corrupt_workers,
            }
        )
        assert executor._max_concurrent == 2
        assert executor._lateral_escalation_enabled is True

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


class TestActiveCheckpointNeverSilentlyAdopted:
    """Round-12 finding #3 (BLOCKING): the round-10 terminal-staleness gate
    distinguishes a crashed run's checkpoint from a FINISHED run's, but not
    from a checkpoint whose writer is STILL ALIVE — another process
    actively running (or legitimately paused/parked) the same seed right
    now. A second, near-concurrent launch of the same seed could adopt the
    live run's execution_id and both processes would race the same
    execution aggregate. Checkpoint saves now embed an ownership marker
    (pid + host + written_at heartbeat, the ``core.worktree`` task-lock
    convention), and recovery refuses — loudly, before any AC work — to
    adopt a non-terminal checkpoint whose owner appears alive."""

    @pytest.mark.asyncio
    async def test_second_launch_refuses_live_owner_then_resumes_after_death(
        self, tmp_path: Path
    ) -> None:
        """The core risk scenario, with a REAL live process as the first
        claimant: launch 2 must fail closed (no dispatch, no adoption, the
        live owner's checkpoint untouched) while the owner lives, and the
        SAME launch must resume the crashed run normally once it is dead."""
        from ouroboros.orchestrator.parallel_executor import CheckpointOwnershipError
        from ouroboros.persistence.checkpoint import CheckpointData

        seed = Seed(
            goal="Concurrent launch ownership",
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

        # --- Run 1: completes level 1 (checkpoint saved with an ownership
        # marker), then is interrupted mid-level-2.
        original_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await original_events.initialize()
        original_ckpt = CheckpointStore(base_path=ckpt_path)
        original_ckpt.initialize()
        original = _executor(event_store=original_events, checkpoint_store=original_ckpt)

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            raise RuntimeError("simulated interruption mid-level-2")

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

        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        owner = saved.value.state["owner"]
        assert owner["pid"] == os.getpid()
        assert owner["host"] == socket.gethostname()

        # --- Stand-in for "the first process is still alive": a genuinely
        # running OS process owns the checkpoint. (The test process itself
        # cannot play both claimants — its own pid is the legitimate
        # same-process relaunch shape — so the checkpoint carries the exact
        # bytes a run inside the live sleeper would have written.)
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        try:
            store = CheckpointStore(base_path=ckpt_path)
            live_state = dict(saved.value.state)
            live_state["owner"] = {**owner, "pid": sleeper.pid}
            assert store.save(
                CheckpointData.create(
                    seed_id=saved.value.seed_id,
                    phase=saved.value.phase,
                    state=live_state,
                )
            ).is_ok

            # --- Launch 2, near-concurrent, same seed + checkpoint store.
            run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
            await run2_events.initialize()
            run2 = _executor(
                event_store=run2_events,
                checkpoint_store=CheckpointStore(base_path=ckpt_path),
            )
            run2._run_batch_with_verify_and_retry = AsyncMock()
            with pytest.raises(CheckpointOwnershipError, match="still active or paused"):
                await run2.execute_parallel(
                    seed,
                    session_id="session-double-launch",
                    execution_id="exec-double-launch",
                    tools=[],
                    system_prompt="system",
                    execution_plan=plan,
                )
            # Fail-closed on every axis: nothing dispatched, nothing
            # adopted, and the LIVE owner's checkpoint is byte-for-byte
            # untouched (no overwrite under launch 2's identity).
            run2._run_batch_with_verify_and_retry.assert_not_awaited()
            preserved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
            assert preserved.is_ok
            assert preserved.value.state["execution_id"] == "exec-original"
            assert preserved.value.state["owner"]["pid"] == sleeper.pid
            await run2_events.close()
        finally:
            sleeper.kill()
            sleeper.wait()

        # --- The owner is now DEAD: the same launch shape is a genuine
        # crash-restart and must resume the interrupted run (level 1
        # skipped, original execution_id restored) — the gate self-heals
        # with no operator override needed.
        run3_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run3_events.initialize()
        run3 = _executor(
            event_store=run3_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        captured: dict[str, object] = {}

        async def _resumed_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            captured["execution_id"] = kwargs["execution_id"]
            captured["batch_executable"] = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            return [ACExecutionResult(ac_index=1, ac_content="Verify integration", success=True)]

        run3._run_batch_with_verify_and_retry = AsyncMock(side_effect=_resumed_stage_runner)
        result = await run3.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=[],
            system_prompt="system",
            execution_plan=plan,
        )
        assert result.all_succeeded
        assert captured["batch_executable"] == [1]
        assert captured["execution_id"] == "exec-original"
        await run3_events.close()

    def _conflict(self, owner: object) -> str | None:
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            cross_harness_redispatch=False,
        )
        cp = SimpleNamespace(
            phase="parallel_execution",
            state={"execution_id": "exec-original", "owner": owner},
        )
        return executor._checkpoint_owner_conflict(cp)

    def test_own_pid_is_never_a_conflict(self) -> None:
        """The same-process relaunch shape (every in-process recovery test
        above) must keep adopting: a process cannot race itself."""
        assert (
            self._conflict(
                {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "written_at": datetime.now(UTC).isoformat(),
                }
            )
            is None
        )

    def test_dead_same_host_pid_is_never_a_conflict(self) -> None:
        """A genuinely exited owner (real subprocess, reaped) is the crash
        shape: resume must proceed even with a fresh heartbeat."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert (
            self._conflict(
                {
                    "pid": proc.pid,
                    "host": socket.gethostname(),
                    "written_at": datetime.now(UTC).isoformat(),
                }
            )
            is None
        )

    def test_cross_host_fresh_heartbeat_is_a_conflict(self) -> None:
        """An unprobeable (different-host) owner with a heartbeat inside
        the freshness window is treated as possibly live."""
        conflict = self._conflict(
            {
                "pid": 12345,
                "host": "some-other-host.example",
                "written_at": datetime.now(UTC).isoformat(),
            }
        )
        assert conflict is not None
        assert "cannot be probed" in conflict

    def test_cross_host_stale_heartbeat_is_not_a_conflict(self) -> None:
        assert (
            self._conflict(
                {
                    "pid": 12345,
                    "host": "some-other-host.example",
                    "written_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                }
            )
            is None
        )

    @pytest.mark.parametrize(
        "owner",
        [
            None,
            "not-a-mapping",
            {},
            {"pid": "12345", "host": "some-other-host.example"},
            {"pid": 12345, "host": "some-other-host.example", "written_at": "not-a-timestamp"},
            {"pid": 12345, "host": "some-other-host.example"},
        ],
    )
    def test_legacy_or_unreadable_owner_keeps_adopt_posture(self, owner: object) -> None:
        """A checkpoint written before this fix (no owner) or with an
        unreadable marker must stay resumable — the one-time-migration
        convention; an unreadable marker must never permanently wall off
        durable ladder history (mirrors ``core.worktree._is_lock_stale``
        treating unreadable timestamps as claimable)."""
        assert self._conflict(owner) is None


class TestInProcessConcurrentSeedExecutionRefused:
    """Round-14 finding #1 (BLOCKING): the round-12 PID gate treats
    ``pid == os.getpid()`` as automatically non-conflicting, so two
    CONCURRENT invocations of the same seed inside ONE long-lived MCP
    server process (separate asyncio tasks, one shared pid) both pass it
    and race the same seed-keyed checkpoint and execution aggregate. The
    in-process lease must make the second concurrent claimant fail loudly
    before any AC work, while the first completes normally — and the lease
    must be released on EVERY exit path so the seed is never permanently
    walled off."""

    @pytest.mark.asyncio
    async def test_second_concurrent_invocation_refused_first_completes(
        self, tmp_path: Path
    ) -> None:
        """The exact review scenario: two near-simultaneous in-flight
        executions of the SAME seed in one test process (real concurrent
        asyncio tasks, not sequential). The loser gets a clear conflict
        error with zero dispatch; the winner finishes normally; the lease
        is then free again for a genuinely sequential relaunch."""
        from ouroboros.orchestrator.parallel_executor import ConcurrentSeedExecutionError

        seed = _seed("Same-process concurrent launch")
        ckpt_path = tmp_path / "checkpoints"
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()

        first_in_flight = asyncio.Event()
        release_first = asyncio.Event()

        async def _blocking_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            first_in_flight.set()
            await release_first.wait()
            return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]

        # Two SEPARATE executor instances, exactly the runner's shape (a
        # fresh executor per invocation) — the lease must still connect
        # them, because it is process-wide, not per-instance.
        run1 = _executor(event_store=AsyncMock(), checkpoint_store=store)
        run1._run_batch_with_verify_and_retry = AsyncMock(side_effect=_blocking_stage_runner)
        run2 = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()

        async def _launch(
            executor: ParallelACExecutor, session_id: str, execution_id: str
        ) -> object:
            return await executor.execute_parallel(
                seed,
                session_id=session_id,
                execution_id=execution_id,
                tools=[],
                system_prompt="system",
                execution_plan=_plan(),
            )

        task1 = asyncio.create_task(_launch(run1, "session-a", "exec-a"))
        # Only race the second launch once the first is GENUINELY mid-AC:
        # past recovery, past the run-start checkpoint save, inside its
        # dispatched batch.
        await asyncio.wait_for(first_in_flight.wait(), timeout=10)
        with pytest.raises(ConcurrentSeedExecutionError, match="already running"):
            await _launch(run2, "session-b", "exec-b")
        # Fail-closed: the loser dispatched nothing.
        run2._run_batch_with_verify_and_retry.assert_not_awaited()
        # The winner is unharmed by the refused claimant.
        release_first.set()
        result = await asyncio.wait_for(task1, timeout=10)
        assert result.all_succeeded
        # Lease released on completion: a sequential relaunch of the same
        # seed must acquire it cleanly (no permanent wall-off).
        run3 = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run3._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
        )
        sequel = await _launch(run3, "session-c", "exec-c")
        assert sequel.all_succeeded

    @pytest.mark.asyncio
    async def test_lease_released_when_invocation_crashes(self, tmp_path: Path) -> None:
        """An exception escaping the run body must still release the lease
        (same every-exit-path ``finally`` as the durable-write drain) —
        otherwise a crashed run would permanently wall off its own seed
        from the legitimate crash-restart the checkpoint exists to serve."""
        seed = _seed("Crash releases in-process lease")
        ckpt_path = tmp_path / "checkpoints"
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()

        crashed = _executor(event_store=AsyncMock(), checkpoint_store=store)
        crashed._run_batch_with_verify_and_retry = AsyncMock(
            side_effect=RuntimeError("simulated interruption")
        )
        with pytest.raises(BaseException):
            await crashed.execute_parallel(
                seed,
                session_id="session-crash",
                execution_id="exec-crash",
                tools=[],
                system_prompt="system",
                execution_plan=_plan(),
            )
        assert seed.metadata.seed_id not in ParallelACExecutor._ACTIVE_SEED_LEASES

        restart = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        restart._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
        )
        result = await restart.execute_parallel(
            seed,
            session_id="session-restart",
            execution_id="exec-restart",
            tools=[],
            system_prompt="system",
            execution_plan=_plan(),
        )
        assert result.all_succeeded

    @pytest.mark.asyncio
    async def test_storeless_concurrent_same_seed_executions_stay_allowed(self) -> None:
        """The lease is scoped to checkpoint-store-backed executions (like
        the round-10/12 gates it complements): without a store there is no
        seed-keyed checkpoint to race — each invocation keeps its own
        execution aggregate, the concurrent-session isolation the session
        layer explicitly supports (see
        ``test_concurrent_session_isolation`` in the e2e suite). Both
        concurrent invocations must succeed."""
        seed = _seed("Store-less concurrency stays allowed")

        gate = asyncio.Event()
        started = asyncio.Event()

        async def _blocking_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            started.set()
            await gate.wait()
            return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]

        run1 = ParallelACExecutor(
            adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
            event_store=AsyncMock(),
            console=MagicMock(),
            cross_harness_redispatch=False,
        )
        run1._run_batch_with_verify_and_retry = AsyncMock(side_effect=_blocking_stage_runner)
        run2 = ParallelACExecutor(
            adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
            event_store=AsyncMock(),
            console=MagicMock(),
            cross_harness_redispatch=False,
        )
        run2._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
        )

        task1 = asyncio.create_task(
            run1.execute_parallel(
                seed,
                session_id="session-a",
                execution_id="exec-a",
                tools=[],
                system_prompt="system",
                execution_plan=_plan(),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=10)
        second = await run2.execute_parallel(
            seed,
            session_id="session-b",
            execution_id="exec-b",
            tools=[],
            system_prompt="system",
            execution_plan=_plan(),
        )
        gate.set()
        first = await asyncio.wait_for(task1, timeout=10)
        assert first.all_succeeded
        assert second.all_succeeded


class TestCheckpointDispatchContract:
    """Recovery may only continue under the original dispatch authority."""

    @staticmethod
    def _seed() -> Seed:
        return Seed(
            goal="Dispatch-contract recovery",
            constraints=(),
            acceptance_criteria=("Prepare workspace", "Apply change"),
            ontology_schema=OntologySchema(name="DispatchContract", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

    @staticmethod
    def _plan() -> StagedExecutionPlan:
        return StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="Prepare workspace"),
                ACNode(index=1, content="Apply change", depends_on=(0,)),
            ),
            stages=(
                ExecutionStage(index=0, ac_indices=(0,)),
                ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
            ),
        )

    async def _crash_after_first_stage(
        self,
        tmp_path: Path,
        *,
        tools: list[str],
        tool_catalog: object = None,
        system_prompt: str = "direct-v1",
        system_prompt_builder: object = None,
        workspace: str = "/tmp/project",
        runtime_backend: str = "claude",
        permission_mode: str | None = None,
        constructor_model: str | None = None,
        capabilities: object = None,
        externally_satisfied_acs: dict[int, dict[str, object]] | None = None,
    ) -> tuple[Seed, Path, dict[str, object]]:
        seed = self._seed()
        ckpt_path = tmp_path / "checkpoints"
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        original = _executor(event_store=AsyncMock(), checkpoint_store=store)
        original._task_cwd = workspace
        original._adapter.runtime_backend = runtime_backend
        original._adapter.permission_mode = permission_mode
        original._adapter._model = constructor_model
        original._adapter.capabilities = capabilities

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [
                    ACExecutionResult(
                        ac_index=0,
                        ac_content="Prepare workspace",
                        success=True,
                    )
                ]
            raise RuntimeError("simulated crash under original dispatch authority")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        with pytest.raises(BaseException, match="original dispatch authority|unhandled errors"):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=tools,
                tool_catalog=tool_catalog,  # type: ignore[arg-type]
                system_prompt=system_prompt,
                execution_plan=self._plan(),
                system_prompt_builder=system_prompt_builder,  # type: ignore[arg-type]
                externally_satisfied_acs=externally_satisfied_acs,  # type: ignore[arg-type]
            )

        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        expected_completed_levels = 0 if externally_satisfied_acs else 1
        assert saved.value.state["completed_levels"] == expected_completed_levels
        return seed, ckpt_path, saved.value.to_dict()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("corruption", ["external_index", "reconciled_index"])
    async def test_dispatch_contract_rejects_out_of_range_ac_identity(
        self,
        tmp_path: Path,
        corruption: str,
    ) -> None:
        from copy import deepcopy

        seed, ckpt_path, _ = await self._crash_after_first_stage(
            tmp_path,
            tools=["Read"],
        )
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        state = deepcopy(saved.value.state)
        dispatch = state["dispatch_contract"]
        if corruption == "external_index":
            dispatch["externally_satisfied_ac_indices"] = [2]
        else:
            dispatch["reconciled_level_contexts"] = [
                {
                    "level_number": 1,
                    "completed_acs": [
                        {
                            "ac_index": 2,
                            "ac_content": "foreign AC",
                            "success": True,
                            "tools_used": [],
                            "files_modified": [],
                            "key_output": "",
                            "public_api": "",
                        }
                    ],
                    "coordinator_review": None,
                }
            ]

        detail = ParallelACExecutor._checkpoint_dispatch_contract_malformed(
            SimpleNamespace(state=state),
            total_acs=2,
            plan_total_stages=2,
        )

        assert detail is not None
        assert "invalid" in detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize("drift", ["tools", "description", "schema", "prompt"])
    async def test_dispatch_authority_drift_is_rejected_before_dispatch(
        self, tmp_path: Path, drift: str
    ) -> None:
        from ouroboros.mcp.types import MCPToolDefinition, MCPToolParameter, ToolInputType

        original_tools = ["Read"]
        resumed_tools = ["Read"]
        original_catalog: tuple[MCPToolDefinition, ...] | None = None
        resumed_catalog: tuple[MCPToolDefinition, ...] | None = None
        original_prompt = "direct-v1"
        resumed_prompt = "direct-v1"

        if drift == "tools":
            original_tools = ["Read", "Write"]
        elif drift in {"description", "schema"}:
            original_tools = resumed_tools = ["custom_tool"]
            original_catalog = (
                MCPToolDefinition(
                    name="custom_tool",
                    description="Original authority",
                    parameters=(MCPToolParameter(name="path", type=ToolInputType.STRING),),
                    server_name="test-server",
                ),
            )
            resumed_catalog = (
                MCPToolDefinition(
                    name="custom_tool",
                    description=(
                        "Changed authority" if drift == "description" else "Original authority"
                    ),
                    parameters=(
                        MCPToolParameter(
                            name="path",
                            type=(
                                ToolInputType.INTEGER if drift == "schema" else ToolInputType.STRING
                            ),
                        ),
                    ),
                    server_name="test-server",
                ),
            )
        else:
            resumed_prompt = "direct-v2"

        seed, ckpt_path, checkpoint_before = await self._crash_after_first_stage(
            tmp_path,
            tools=original_tools,
            tool_catalog=original_catalog,
            system_prompt=original_prompt,
        )
        recovered = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        recovered._run_batch_with_verify_and_retry = AsyncMock()

        with pytest.raises(CheckpointDispatchMismatchError):
            await recovered.execute_parallel(
                seed,
                session_id="session-restarted",
                execution_id="exec-restarted",
                tools=resumed_tools,
                tool_catalog=resumed_catalog,
                system_prompt=resumed_prompt,
                execution_plan=self._plan(),
            )

        recovered._run_batch_with_verify_and_retry.assert_not_awaited()
        checkpoint_after = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert checkpoint_after.is_ok
        assert checkpoint_after.value.to_dict() == checkpoint_before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "drift",
        ["workspace", "runtime_backend", "permission_mode", "constructor_model", "capabilities"],
    )
    async def test_runtime_authority_drift_is_rejected_before_dispatch(
        self, tmp_path: Path, drift: str
    ) -> None:
        from ouroboros.orchestrator.adapter import ParamSupport, RuntimeCapabilities

        original_workspace = str(tmp_path / "workspace-a")
        resumed_workspace = original_workspace
        original_backend = resumed_backend = "claude"
        original_permission = resumed_permission = "acceptEdits"
        original_model = resumed_model = "model-a"
        original_capabilities = resumed_capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            model_override_support=ParamSupport.NATIVE,
        )
        if drift == "workspace":
            resumed_workspace = str(tmp_path / "workspace-b")
        elif drift == "runtime_backend":
            resumed_backend = "codex_cli"
        elif drift == "permission_mode":
            resumed_permission = "bypassPermissions"
        elif drift == "constructor_model":
            resumed_model = "model-b"
        else:
            resumed_capabilities = RuntimeCapabilities(
                skill_dispatch=True,
                targeted_resume=True,
                structured_output=True,
                model_override_support=ParamSupport.IGNORED,
            )

        seed, ckpt_path, _checkpoint_before = await self._crash_after_first_stage(
            tmp_path,
            tools=["Read"],
            workspace=original_workspace,
            runtime_backend=original_backend,
            permission_mode=original_permission,
            constructor_model=original_model,
            capabilities=original_capabilities,
        )
        recovered = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        recovered._task_cwd = resumed_workspace
        recovered._adapter.runtime_backend = resumed_backend
        recovered._adapter.permission_mode = resumed_permission
        recovered._adapter._model = resumed_model
        recovered._adapter.capabilities = resumed_capabilities
        recovered._run_batch_with_verify_and_retry = AsyncMock()

        with pytest.raises(CheckpointDispatchMismatchError):
            await recovered.execute_parallel(
                seed,
                session_id="session-restarted",
                execution_id="exec-restarted",
                tools=["Read"],
                system_prompt="direct-v1",
                execution_plan=self._plan(),
            )

        recovered._run_batch_with_verify_and_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_identical_direct_contract_resumes_only_unfinished_work(
        self, tmp_path: Path
    ) -> None:
        seed, ckpt_path, _ = await self._crash_after_first_stage(
            tmp_path,
            tools=["Read"],
            system_prompt="direct-v1",
        )
        recovered = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        dispatched: list[list[int]] = []

        async def _finish(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            dispatched.append(batch)
            return [ACExecutionResult(ac_index=1, ac_content="Apply change", success=True)]

        recovered._run_batch_with_verify_and_retry = AsyncMock(side_effect=_finish)
        result = await recovered.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=["Read"],
            system_prompt="direct-v1",
            execution_plan=self._plan(),
        )

        assert dispatched == [[1]]
        assert result.execution_id == "exec-original"
        assert result.all_succeeded

    @pytest.mark.asyncio
    async def test_recovery_restores_original_externally_satisfied_set(
        self, tmp_path: Path
    ) -> None:
        seed, ckpt_path, _ = await self._crash_after_first_stage(
            tmp_path,
            tools=["Read"],
            system_prompt="direct-v1",
            externally_satisfied_acs={0: {"reason": "already present"}},
        )
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["dispatch_contract"]["externally_satisfied_ac_indices"] == [0]

        recovered = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        dispatched: list[list[int]] = []

        async def _finish(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            dispatched.append(batch)
            return [ACExecutionResult(ac_index=1, ac_content="Apply change", success=True)]

        recovered._run_batch_with_verify_and_retry = AsyncMock(side_effect=_finish)
        result = await recovered.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=["Read"],
            system_prompt="direct-v1",
            execution_plan=self._plan(),
            externally_satisfied_acs={},
        )

        assert dispatched == [[1]]
        assert result.externally_satisfied_count == 1
        assert result.all_succeeded

    @pytest.mark.asyncio
    async def test_mixed_external_and_executed_stage_writes_valid_progress(
        self, tmp_path: Path
    ) -> None:
        seed = self._seed()
        plan = StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="Prepare workspace"),
                ACNode(index=1, content="Apply change"),
            ),
            stages=(ExecutionStage(index=0, ac_indices=(0, 1)),),
        )
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        executor = _executor(event_store=AsyncMock(), checkpoint_store=store)
        executor._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[ACExecutionResult(ac_index=1, ac_content="Apply change", success=True)]
        )

        result = await executor.execute_parallel(
            seed,
            session_id="session-mixed-external",
            execution_id="exec-mixed-external",
            tools=["Read"],
            system_prompt="direct-v1",
            execution_plan=plan,
            externally_satisfied_acs={0: {"reason": "already present"}},
        )

        assert result.all_succeeded
        saved = store.load(seed.metadata.seed_id)
        assert saved.is_ok
        state = saved.value.state
        assert state["dispatch_contract"]["externally_satisfied_ac_indices"] == [0]
        assert state["satisfied_externally_indices"] == [0]
        assert [
            summary["ac_index"]
            for context in state["level_contexts"]
            for summary in context["completed_acs"]
        ] == [1]
        assert ParallelACExecutor._checkpoint_progress_malformed(saved.value, total_acs=2) is None

    @pytest.mark.asyncio
    async def test_mixed_external_checkpoint_resumes_only_downstream_work(
        self, tmp_path: Path
    ) -> None:
        seed = Seed(
            goal="Resume mixed external progress",
            constraints=(),
            acceptance_criteria=("External setup", "Executed setup", "Downstream work"),
            ontology_schema=OntologySchema(name="ExternalResume", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )
        plan = StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="External setup"),
                ACNode(index=1, content="Executed setup"),
                ACNode(index=2, content="Downstream work", depends_on=(0, 1)),
            ),
            stages=(
                ExecutionStage(index=0, ac_indices=(0, 1)),
                ExecutionStage(index=1, ac_indices=(2,), depends_on_stages=(0,)),
            ),
        )
        ckpt_path = tmp_path / "checkpoints"
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        original = _executor(event_store=AsyncMock(), checkpoint_store=store)

        async def _crash_downstream(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [1]:
                return [ACExecutionResult(ac_index=1, ac_content="Executed setup", success=True)]
            raise RuntimeError("crash after mixed stage checkpoint")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crash_downstream)
        with pytest.raises(BaseException, match="mixed stage checkpoint|unhandled errors"):
            await original.execute_parallel(
                seed,
                session_id="session-mixed-original",
                execution_id="exec-mixed-original",
                tools=["Read"],
                system_prompt="direct-v1",
                execution_plan=plan,
                externally_satisfied_acs={0: {"reason": "already present"}},
            )

        recovered = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        dispatched: list[list[int]] = []

        async def _finish_downstream(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            dispatched.append(batch)
            return [ACExecutionResult(ac_index=2, ac_content="Downstream work", success=True)]

        recovered._run_batch_with_verify_and_retry = AsyncMock(side_effect=_finish_downstream)
        result = await recovered.execute_parallel(
            seed,
            session_id="session-mixed-restarted",
            execution_id="exec-mixed-restarted",
            tools=["Read"],
            system_prompt="direct-v1",
            execution_plan=plan,
            externally_satisfied_acs={},
        )

        assert dispatched == [[2]]
        assert result.externally_satisfied_count == 1
        assert result.results[0].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        assert result.all_succeeded

    @pytest.mark.asyncio
    async def test_mixed_external_stage_resumes_downstream_without_external_context(
        self, tmp_path: Path
    ) -> None:
        seed = Seed(
            goal="Resume after mixed external stage",
            constraints=(),
            acceptance_criteria=("Already present", "Prepare", "Apply"),
            ontology_schema=OntologySchema(name="MixedResume", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )
        plan = StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="Already present"),
                ACNode(index=1, content="Prepare"),
                ACNode(index=2, content="Apply", depends_on=(0, 1)),
            ),
            stages=(
                ExecutionStage(index=0, ac_indices=(0, 1)),
                ExecutionStage(index=1, ac_indices=(2,), depends_on_stages=(0,)),
            ),
        )
        ckpt_path = tmp_path / "checkpoints"
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        original = _executor(event_store=AsyncMock(), checkpoint_store=store)

        async def _crash_downstream(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [1]:
                return [ACExecutionResult(ac_index=1, ac_content="Prepare", success=True)]
            raise RuntimeError("crash before downstream stage completes")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crash_downstream)
        with pytest.raises(BaseException, match="downstream stage|unhandled errors"):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=["Read"],
                system_prompt="direct-v1",
                execution_plan=plan,
                externally_satisfied_acs={0: {"reason": "already present"}},
            )

        recovered = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        dispatched: list[list[int]] = []

        async def _finish(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            dispatched.append(batch)
            return [ACExecutionResult(ac_index=2, ac_content="Apply", success=True)]

        recovered._run_batch_with_verify_and_retry = AsyncMock(side_effect=_finish)
        result = await recovered.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=["Read"],
            system_prompt="direct-v1",
            execution_plan=plan,
            externally_satisfied_acs={},
        )

        assert dispatched == [[2]]
        assert result.externally_satisfied_count == 1
        assert result.all_succeeded

    @pytest.mark.asyncio
    async def test_reconciled_run_start_anchor_is_valid_with_pending_status(
        self, tmp_path: Path
    ) -> None:
        seed = _seed("Reconciled run-start anchor")
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        executor = _executor(event_store=AsyncMock(), checkpoint_store=store)
        executor._run_batch_with_verify_and_retry = AsyncMock(
            side_effect=RuntimeError("crash before first stage completes")
        )
        handoff = LevelContext(
            level_number=1,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Parent work from prior attempt",
                    success=True,
                    tools_used=("Edit",),
                    files_modified=("shared.py",),
                    key_output="workspace reconciled",
                ),
            ),
        )

        with pytest.raises(BaseException, match="crash before first stage|unhandled errors"):
            await executor.execute_parallel(
                seed,
                session_id="session-reconciled-anchor",
                execution_id="exec-reconciled-anchor",
                tools=["Read"],
                system_prompt="direct-v1",
                execution_plan=_plan(),
                reconciled_level_contexts=[handoff],
            )

        saved = store.load(seed.metadata.seed_id)
        assert saved.is_ok
        state = saved.value.state
        assert state["ac_statuses"] == {"0": "pending"}
        assert state["dispatch_contract"]["reconciled_level_contexts"] == state["level_contexts"]
        assert ParallelACExecutor._checkpoint_progress_malformed(saved.value, total_acs=1) is None

    @pytest.mark.asyncio
    async def test_rebuildable_prompt_contract_rebuilds_after_recovery(
        self, tmp_path: Path
    ) -> None:
        seed, ckpt_path, _ = await self._crash_after_first_stage(
            tmp_path,
            tools=["Read"],
            system_prompt="original prebuilt prompt",
            system_prompt_builder=lambda **_kwargs: "original rebuilt prompt",
        )
        recovered = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        builder = MagicMock(return_value="recovered rebuilt prompt")
        captured: dict[str, object] = {}

        async def _finish(**kwargs: object) -> list[ACExecutionResult]:
            captured["system_prompt"] = kwargs["system_prompt"]
            return [ACExecutionResult(ac_index=1, ac_content="Apply change", success=True)]

        recovered._run_batch_with_verify_and_retry = AsyncMock(side_effect=_finish)
        result = await recovered.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=["Read"],
            system_prompt="different stale prebuilt prompt",
            execution_plan=self._plan(),
            system_prompt_builder=builder,
        )

        assert result.all_succeeded
        builder.assert_called_once()
        assert captured["system_prompt"] == "recovered rebuilt prompt"


class TestRecoveredPromptReflectsOriginalRunSemantics:
    """Round-14 finding #3 (BLOCKING): the runner builds the system prompt
    BEFORE ``execute_parallel`` can run RC3 checkpoint recovery, so the
    prompt handed in was baked from the CURRENT process's prompt semantics
    (fat-harness strategy, context-pack flag, guidance) — even a complete
    field-coverage restore could not fix the already-constructed prompt.
    The executor must now call the runner's ``system_prompt_builder`` back
    AFTER restoration (and only on a genuinely adopted recovery) with the
    RESTORED semantics, and dispatch the rebuilt prompt."""

    _GUIDANCE = {
        "mode": "disabled",
        "provenance_scope": "ouroboros_declared_guidance_only",
        "items": [],
    }

    async def _crash_original_run(
        self, seed: Seed, ckpt_path: Path, **executor_overrides: object
    ) -> None:
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        original = _executor(
            event_store=AsyncMock(),
            checkpoint_store=store,
            **executor_overrides,  # type: ignore[arg-type]
        )
        original._run_batch_with_verify_and_retry = AsyncMock(
            side_effect=RuntimeError("simulated crash mid-level-1")
        )
        with pytest.raises(BaseException):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="original-process prompt",
                execution_plan=_plan(),
                system_prompt_builder=lambda **_kwargs: "original-process prompt",
            )

    @pytest.mark.asyncio
    async def test_dispatched_prompt_is_rebuilt_from_restored_settings(
        self, tmp_path: Path
    ) -> None:
        """The review's scenario: the checkpointed run's context-pack/
        guidance/fat-harness settings genuinely DIFFER from the restarted
        process's config. The prompt actually dispatched must reflect the
        RESTORED (original) settings, not the stale prompt the caller baked
        from the current process's config."""
        seed = _seed("Recovered prompt semantics")
        ckpt_path = tmp_path / "checkpoints"
        await self._crash_original_run(
            seed,
            ckpt_path,
            fat_harness_mode=True,
            context_pack_enabled=False,
            prompt_guidance_contract=self._GUIDANCE,
        )

        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        semantics = saved.value.state["execution_semantics"]
        assert semantics["fat_harness_mode"] is True
        assert semantics["context_pack_enabled"] is False
        assert saved.value.state["prompt_guidance"] == self._GUIDANCE

        # Restart in a process configured the OPPOSITE way.
        restart = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            fat_harness_mode=False,
            context_pack_enabled=True,
            prompt_guidance_contract=None,
        )
        builder_calls: list[dict[str, object]] = []

        def _builder(**kwargs: object) -> str:
            builder_calls.append(kwargs)
            return "REBUILT-FROM-RESTORED-SETTINGS"

        captured: dict[str, object] = {}

        async def _capturing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            captured["system_prompt"] = kwargs["system_prompt"]
            return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]

        restart._run_batch_with_verify_and_retry = AsyncMock(side_effect=_capturing_stage_runner)
        result = await restart.execute_parallel(
            seed,
            session_id="session-restart",
            execution_id="exec-restart",
            tools=[],
            system_prompt="STALE prompt baked from the current process's config",
            execution_plan=_plan(),
            system_prompt_builder=_builder,
        )

        assert result.all_succeeded
        # The builder ran exactly once, AFTER restoration, with the
        # ORIGINAL run's semantics — not the restart process's.
        assert builder_calls == [
            {
                "fat_harness_mode": True,
                "context_pack_enabled": False,
                "guidance_contract": self._GUIDANCE,
                "execution_profile": None,
            }
        ]
        # And the rebuilt prompt — not the stale baked one — was dispatched.
        assert captured["system_prompt"] == "REBUILT-FROM-RESTORED-SETTINGS"

    @pytest.mark.asyncio
    async def test_checkpoint_restores_full_execution_profile_before_prompt_build(
        self, tmp_path: Path
    ) -> None:
        seed = _seed("Recovered execution profile")
        original_profile = load_profile("code")
        changed_profile = original_profile.model_copy(update={"axis": "changed-axis"})
        ckpt_path = tmp_path / "checkpoints"
        await self._crash_original_run(
            seed,
            ckpt_path,
            execution_profile=original_profile,
        )

        restart = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            execution_profile=changed_profile,
        )
        builder = MagicMock(return_value="restored-profile-prompt")
        restart._run_batch_with_verify_and_retry = AsyncMock(
            return_value=[ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
        )

        result = await restart.execute_parallel(
            seed,
            session_id="session-restart",
            execution_id="exec-restart",
            tools=[],
            system_prompt="stale-profile-prompt",
            execution_plan=_plan(),
            system_prompt_builder=builder,
        )

        assert result.all_succeeded
        assert restart._execution_profile == original_profile
        assert builder.call_args.kwargs["execution_profile"] == original_profile

    @pytest.mark.asyncio
    async def test_fresh_run_keeps_caller_prompt_and_never_calls_builder(
        self, tmp_path: Path
    ) -> None:
        """No checkpoint adopted: the caller's prompt is used byte-for-byte
        and the builder is never invoked (no behavior change for fresh
        runs)."""
        seed = _seed("Fresh run keeps baked prompt")
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        fresh = _executor(
            event_store=AsyncMock(),
            checkpoint_store=store,
            context_pack_enabled=True,
        )
        builder = MagicMock(return_value="never-used")
        captured: dict[str, object] = {}

        async def _capturing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            captured["system_prompt"] = kwargs["system_prompt"]
            return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]

        fresh._run_batch_with_verify_and_retry = AsyncMock(side_effect=_capturing_stage_runner)
        result = await fresh.execute_parallel(
            seed,
            session_id="session-fresh",
            execution_id="exec-fresh",
            tools=[],
            system_prompt="freshly baked prompt",
            execution_plan=_plan(),
            system_prompt_builder=builder,
        )

        assert result.all_succeeded
        builder.assert_not_called()
        assert captured["system_prompt"] == "freshly baked prompt"

    @pytest.mark.asyncio
    async def test_builder_refusal_fails_launch_before_any_dispatch(self, tmp_path: Path) -> None:
        """A builder raise (the runner's guidance identity check refusing
        changed guidance) must fail the recovered launch loudly BEFORE any
        AC work — never degrade into dispatching the stale prompt."""
        seed = _seed("Guidance refusal fails launch")
        ckpt_path = tmp_path / "checkpoints"
        await self._crash_original_run(
            seed,
            ckpt_path,
            context_pack_enabled=False,
            prompt_guidance_contract=self._GUIDANCE,
        )

        restart = _executor(
            event_store=AsyncMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            context_pack_enabled=True,
        )
        restart._run_batch_with_verify_and_retry = AsyncMock()

        def _refusing_builder(**kwargs: object) -> str:
            raise RuntimeError("Cannot resume because declared project guidance changed")

        with pytest.raises(RuntimeError, match="guidance changed"):
            await restart.execute_parallel(
                seed,
                session_id="session-restart",
                execution_id="exec-restart",
                tools=[],
                system_prompt="STALE prompt",
                execution_plan=_plan(),
                system_prompt_builder=_refusing_builder,
            )
        restart._run_batch_with_verify_and_retry.assert_not_awaited()


def _seed_three_acs(goal: str = "Plan regrouping resume") -> Seed:
    return Seed(
        goal=goal,
        constraints=(),
        acceptance_criteria=("Build core", "Add API", "Write docs"),
        ontology_schema=OntologySchema(name="Replan", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _three_level_plan() -> StagedExecutionPlan:
    return StagedExecutionPlan(
        nodes=(
            ACNode(index=0, content="Build core"),
            ACNode(index=1, content="Add API", depends_on=(0,)),
            ACNode(index=2, content="Write docs", depends_on=(1,)),
        ),
        stages=(
            ExecutionStage(index=0, ac_indices=(0,)),
            ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
            ExecutionStage(index=2, ac_indices=(2,), depends_on_stages=(1,)),
        ),
    )


def _collapsed_fallback_plan(seed: Seed) -> StagedExecutionPlan:
    """The EXACT plan shape the runner's dependency-analysis failure
    fallback produces: every AC collapsed into ONE single level, no
    dependencies (see ``runner.py`` ``dependency_analysis_failed``)."""
    all_indices = tuple(range(len(seed.acceptance_criteria)))
    graph = DependencyGraph(
        nodes=tuple(
            ACNode(index=i, content=str(ac), depends_on=())
            for i, ac in enumerate(seed.acceptance_criteria)
        ),
        execution_levels=(all_indices,) if all_indices else (),
    )
    return graph.to_execution_plan()


class TestResumeRejectsPlanRegrouping:
    """Round-16 finding #1 (BLOCKING): ``completed_levels`` is an integer
    relative to the ORIGINAL run's plan STRUCTURE, but the plan is re-derived
    by LLM dependency analysis on every launch — including a documented
    deterministic fallback that collapses ALL ACs into one single level when
    the analysis fails. The round-15 seed fingerprint deliberately excludes
    plan structure (the plan may legitimately differ across a genuine
    resume), so recovery could adopt ``completed_levels`` from a 3-level run
    and apply it against a 1-level re-derived plan: the entire collapsed
    plan was skipped wholesale and every never-dispatched AC was
    reconstructed as "Failed (restored from checkpoint)" — FAILED with zero
    dispatch and zero escalation, the forbidden outcome. Resume must key off
    per-AC statuses (stable AC index), not a plan-relative level count."""

    @pytest.mark.asyncio
    async def test_collapsed_replan_is_rejected_before_dispatch(self, tmp_path: Path) -> None:
        """The review's exact scenario: original 3-level plan, crash after
        level 1 (``completed_levels=1`` checkpointed), resume of the SAME
        (fingerprint-matching) seed content under the analyzer-fallback's
        single collapsed level. ACs 1 and 2 were never dispatched by the
        original run — they must be dispatched on resume, never surfaced as
        FAILED from checkpoint bookkeeping alone."""
        seed = _seed_three_acs()
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"

        # --- Original run: 3-level plan; level 1 completes (checkpoint
        # saved with completed_levels=1), then the process crashes during
        # level 2.
        original_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await original_events.initialize()
        original_ckpt = CheckpointStore(base_path=ckpt_path)
        original_ckpt.initialize()
        original = _executor(event_store=original_events, checkpoint_store=original_ckpt)

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Build core", success=True)]
            raise RuntimeError("simulated process crash during level 2")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        with pytest.raises(BaseException):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=_three_level_plan(),
            )
        await original_events.close()

        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["completed_levels"] == 1
        assert saved.value.state["ac_statuses"] == {
            "0": "completed",
            "1": "pending",
            "2": "pending",
        }

        # --- Resume: SAME seed content (the round-15 fingerprint gate
        # passes), but this launch's dependency analysis fell back to the
        # single collapsed level.
        collapsed = _collapsed_fallback_plan(seed)
        assert collapsed.total_stages == 1

        recovered_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await recovered_events.initialize()
        recovered = _executor(
            event_store=recovered_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        recovered._run_batch_with_verify_and_retry = AsyncMock()
        with pytest.raises(CheckpointPlanMismatchError):
            await recovered.execute_parallel(
                seed,
                session_id="session-restarted",
                execution_id="exec-restarted",
                tools=[],
                system_prompt="system",
                execution_plan=collapsed,
            )
        await recovered_events.close()
        recovered._run_batch_with_verify_and_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_indeterminate_replay_full_completion_judged_against_original_plan_shape(
        self, tmp_path: Path
    ) -> None:
        """The round-13 full-completion tiebreaker is the same class of
        defect: it compared the checkpoint's ``completed_levels`` against
        THIS launch's re-derived stage count. A finished 1-level run's
        leftover checkpoint (delete failed) must stay refused on an
        indeterminate replay even when this launch re-derived a plan with
        MORE stages — the checkpoint's own recorded plan size is the
        authoritative comparand."""
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        executor = _executor(event_store=AsyncMock(), checkpoint_store=store)

        async def _indeterminate(*_args: object, **_kwargs: object) -> None:
            return None

        executor._replay_with_retry = _indeterminate  # type: ignore[method-assign]
        cp = SimpleNamespace(
            phase="parallel_execution",
            state={
                "execution_id": "exec-finished",
                "completed_levels": 1,
                "plan_total_stages": 1,
            },
        )
        assert await executor._checkpoint_from_terminal_run(cp, total_levels=3) is True


def _seed_four_acs(goal: str = "Skipped AC re-evaluation") -> Seed:
    return Seed(
        goal=goal,
        constraints=(),
        acceptance_criteria=(
            "Build base",
            "Integrate feature",
            "Independent work",
            "Final verification",
        ),
        ontology_schema=OntologySchema(name="SkipCascade", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _skip_cascade_plan(*, feature_depends_on_base: bool) -> StagedExecutionPlan:
    """Stage shape [0], [1, 2], [3]; the 1->0 dependency edge (the one that
    caused the original cascade skip) is present or dropped per the flag —
    modelling a re-derived plan whose dependency structure changed."""
    return StagedExecutionPlan(
        nodes=(
            ACNode(index=0, content="Build base"),
            ACNode(
                index=1,
                content="Integrate feature",
                depends_on=(0,) if feature_depends_on_base else (),
            ),
            ACNode(index=2, content="Independent work"),
            ACNode(index=3, content="Final verification"),
        ),
        stages=(
            ExecutionStage(index=0, ac_indices=(0,)),
            ExecutionStage(index=1, ac_indices=(1, 2), depends_on_stages=(0,)),
            ExecutionStage(index=2, ac_indices=(3,), depends_on_stages=(1,)),
        ),
    )


class TestSkippedACsReevaluatedOnResume:
    """Round-17 finding #2 (BLOCKING): an AC recorded as ``skipped`` never
    RAN — it was withheld purely because an upstream dependency of the
    ORIGINAL run's plan failed. That is a plan-structure-relative fact,
    and the plan is re-derived by dependency analysis on every launch
    (round-16 #1 already established plan-relative checkpoint state is
    unsafe to trust across a re-derived plan). Treating ``skipped`` as a
    permanently-resolved terminal state let a resumed run surface a
    failed-shaped "Skipped: dependency failed" result with ZERO dispatch
    even when the re-derived plan no longer contains the edge that caused
    the skip — the forbidden outcome. ``skipped`` must be re-opened on
    restore so the CURRENT plan's dependency cascade re-decides it: a
    still-failed dependency re-skips it identically, a vanished edge (or
    a dependency that succeeds this time) gives it the genuine dispatch
    it never had."""

    async def _crash_after_skip_recorded(self, tmp_path: Path, seed: Seed) -> Path:
        """Run 1 under the edge-bearing plan: AC0 fails (level 1), so AC1 is
        cascade-skipped while AC2 succeeds (level 2 completes — its
        checkpoint durably records AC1 as ``skipped``), then the process
        crashes dispatching AC3 (level 3)."""
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"
        original_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await original_events.initialize()
        original_ckpt = CheckpointStore(base_path=ckpt_path)
        original_ckpt.initialize()
        original = _executor(event_store=original_events, checkpoint_store=original_ckpt)

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [
                    ACExecutionResult(
                        ac_index=0,
                        ac_content="Build base",
                        success=False,
                        error="base build failed",
                    )
                ]
            if batch == [2]:
                return [ACExecutionResult(ac_index=2, ac_content="Independent work", success=True)]
            raise RuntimeError("simulated process crash during level 3")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        with pytest.raises(BaseException, match="simulated process crash|unhandled errors"):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=_skip_cascade_plan(feature_depends_on_base=True),
            )
        await original_events.close()

        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        # The review's precondition: the cascade skip IS durably recorded.
        assert saved.value.state["ac_statuses"] == {
            "0": "failed",
            "1": "skipped",
            "2": "completed",
            "3": "pending",
        }
        assert saved.value.state["failed_indices"] == [0]
        return ckpt_path

    async def _resume(
        self, tmp_path: Path, ckpt_path: Path, seed: Seed, plan: StagedExecutionPlan
    ) -> tuple[list[list[int]], object]:
        recovered_events = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
        await recovered_events.initialize()
        recovered = _executor(
            event_store=recovered_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        dispatched: list[list[int]] = []

        async def _recovered_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            dispatched.append(batch)
            return [
                ACExecutionResult(
                    ac_index=idx,
                    ac_content=str(seed.acceptance_criteria[idx]),
                    success=True,
                )
                for idx in batch
            ]

        recovered._run_batch_with_verify_and_retry = AsyncMock(side_effect=_recovered_stage_runner)
        result = await recovered.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=[],
            system_prompt="system",
            execution_plan=plan,
        )
        await recovered_events.close()
        return dispatched, result

    @pytest.mark.asyncio
    async def test_replan_that_drops_failed_edge_is_rejected_before_dispatch(
        self, tmp_path: Path
    ) -> None:
        """The review's exact scenario: AC1 was skipped ONLY because run 1's
        plan made it depend on the failed AC0. The resumed launch re-derived
        a plan WITHOUT that edge, so AC1's prerequisite no longer exists —
        it must get the genuine dispatch it never had (and here succeeds),
        never stay permanently skipped off the stale dependency snapshot."""
        seed = _seed_four_acs()
        ckpt_path = await self._crash_after_skip_recorded(tmp_path, seed)

        with pytest.raises(CheckpointPlanMismatchError):
            await self._resume(
                tmp_path,
                ckpt_path,
                seed,
                _skip_cascade_plan(feature_depends_on_base=False),
            )

    @pytest.mark.asyncio
    async def test_skipped_ac_is_reskipped_identically_when_dependency_still_failed(
        self, tmp_path: Path
    ) -> None:
        """Control: when the re-derived plan KEEPS the 1->0 edge and AC0 is
        still terminally failed, re-evaluating AC1 under the current plan's
        cascade re-skips it identically — re-opening ``skipped`` never
        re-dispatches a genuinely-still-blocked AC."""
        seed = _seed_four_acs()
        ckpt_path = await self._crash_after_skip_recorded(tmp_path, seed)

        dispatched, result = await self._resume(
            tmp_path, ckpt_path, seed, _skip_cascade_plan(feature_depends_on_base=True)
        )

        # Only AC3 (never reached by run 1) is dispatched; AC1 is freshly
        # re-skipped by the cascade, not dispatched.
        assert dispatched == [[3]]
        by_index = {r.ac_index: r for r in result.results}
        assert by_index[1].success is False
        assert by_index[1].error == "Skipped: dependency failed"
        assert by_index[0].error == "Failed (restored from checkpoint)"
        assert by_index[3].success is True
        assert result.execution_id == "exec-original"


class TestOrdinaryRetryBudgetSurvivesCrashRestart:
    """Round-17 finding #4 (BLOCKING): the per-AC ordinary (pre-ladder,
    same-runtime) retry counter ``ac_retry_attempts`` lived only in memory
    and reset to zero on every launch — and checkpoints cannot carry it:
    they save per-level, so retries consumed INSIDE the level a crash
    interrupts are never in any checkpoint. Only ACs already escalated
    into the ladder had a durable attempt record (round-6 #1's
    ``progressed`` seeding), so an AC that crashed MID ordinary-retry
    resumed with a FRESH zero budget: non-idempotent work re-run beyond
    the configured cap, and attempt-number-keyed correlation reset onto
    already-used ordinals. The consumption is now RECONSTRUCTED from the
    original run's durable ``execution.ac.outcome_finalized`` markers
    (emitted after EVERY attempt's verify gate — the same replay-on-resume
    convention the ladder uses), so a resumed AC re-enters at the next
    attempt after its highest durably-finalized one."""

    @staticmethod
    def _two_ac_seed() -> Seed:
        return Seed(
            goal="Ordinary retry budget survives crash",
            constraints=(),
            acceptance_criteria=("Prepare data", "Apply migration"),
            ontology_schema=OntologySchema(name="RetryBudget", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )

    @staticmethod
    def _two_stage_plan() -> StagedExecutionPlan:
        return StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="Prepare data"),
                ACNode(index=1, content="Apply migration", depends_on=(0,)),
            ),
            stages=(
                ExecutionStage(index=0, ac_indices=(0,)),
                ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
            ),
        )

    @pytest.mark.asyncio
    async def test_malformed_newer_finalized_marker_blocks_stale_fallback(self) -> None:
        """A corrupt latest attempt cannot make an older attempt authoritative."""
        from ouroboros.events.base import BaseEvent

        event_store = AsyncMock()
        event_store.replay.return_value = [
            BaseEvent(
                type="execution.ac.outcome_finalized",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "retry_attempt": 0,
                    "success": False,
                    "outcome": "failed",
                    "is_decomposed": False,
                    "forced_frontier_routing": False,
                    "context_summary": None,
                },
            ),
            BaseEvent(
                type="execution.ac.outcome_finalized",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "retry_attempt": 1,
                    # Type-mangled latest success from a torn/foreign writer.
                    "success": "true",
                    "outcome": "succeeded",
                    "is_decomposed": False,
                    "forced_frontier_routing": False,
                    "context_summary": None,
                },
            ),
        ]
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        recovered = await executor._reconstruct_finalized_outcomes(
            execution_id="exec-original",
            total_acs=1,
        )

        assert recovered is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("success", "outcome"),
        [
            (False, "succeeded"),
            (True, "failed"),
            (False, "satisfied_externally"),
            (True, "blocked"),
        ],
    )
    async def test_contradictory_finalized_outcome_fails_closed(
        self,
        success: bool,
        outcome: str,
    ) -> None:
        from ouroboros.events.base import BaseEvent

        event_store = AsyncMock()
        event_store.replay.return_value = [
            BaseEvent(
                type="execution.ac.outcome_finalized",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "ac_index": 0,
                    "retry_attempt": 0,
                    "success": success,
                    "outcome": outcome,
                    "is_decomposed": False,
                    "forced_frontier_routing": False,
                    "context_summary": None,
                },
            )
        ]
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        recovered = await executor._reconstruct_finalized_outcomes(
            execution_id="exec-original",
            total_acs=1,
        )

        assert recovered is None

    @pytest.mark.asyncio
    async def test_same_attempt_contradictory_finalized_markers_fail_closed(self) -> None:
        """One attempt cannot durably finalize as both success and failure."""
        from ouroboros.events.base import BaseEvent

        def _marker(*, success: bool, outcome: str) -> BaseEvent:
            return BaseEvent(
                type="execution.ac.outcome_finalized",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "ac_index": 0,
                    "retry_attempt": 0,
                    "success": success,
                    "outcome": outcome,
                    "is_decomposed": False,
                    "forced_frontier_routing": False,
                    "context_summary": None,
                },
            )

        event_store = AsyncMock()
        event_store.replay.return_value = [
            _marker(success=True, outcome="succeeded"),
            _marker(success=False, outcome="failed"),
        ]
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        recovered = await executor._reconstruct_finalized_outcomes(
            execution_id="exec-original",
            total_acs=1,
        )

        assert recovered is None

    @pytest.mark.asyncio
    async def test_same_attempt_identical_finalized_markers_are_idempotent(self) -> None:
        """A retried append of the same durable marker is harmless."""
        from ouroboros.events.base import BaseEvent

        marker = BaseEvent(
            type="execution.ac.outcome_finalized",
            aggregate_type="execution",
            aggregate_id="exec-original",
            data={
                "execution_id": "exec-original",
                "session_id": "s1",
                "root_ac_index": 0,
                "ac_index": 0,
                "retry_attempt": 0,
                "success": True,
                "outcome": "succeeded",
                "is_decomposed": False,
                "forced_frontier_routing": False,
                "context_summary": None,
            },
        )
        event_store = AsyncMock()
        event_store.replay.return_value = [marker, marker]
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        recovered = await executor._reconstruct_finalized_outcomes(
            execution_id="exec-original",
            total_acs=1,
        )

        assert recovered is not None
        assert recovered[0].success is True
        assert recovered[0].retry_attempt == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "corruption",
        [
            {"success": True},
            {"schema_version": 2},
            {"configured_retry_attempts": 99},
            {"retry_termination_reason": "unknown_reason"},
            {"alternate_redispatch_status": "succeeded"},
            {
                "retry_attempt": 0,
                "retry_termination_reason": "budget_exhausted",
            },
        ],
    )
    async def test_malformed_recovery_exhausted_payload_fails_closed(
        self,
        corruption: dict[str, object],
    ) -> None:
        from ouroboros.events.base import BaseEvent

        finalized = BaseEvent(
            type="execution.ac.outcome_finalized",
            aggregate_type="execution",
            aggregate_id="exec-original",
            data={
                "execution_id": "exec-original",
                "session_id": "s1",
                "root_ac_index": 0,
                "ac_index": 0,
                "retry_attempt": 0,
                "success": False,
                "outcome": "failed",
                "is_decomposed": False,
                "forced_frontier_routing": False,
                "context_summary": None,
            },
        )
        closure_data: dict[str, object] = {
            "schema_version": 1,
            "execution_id": "exec-original",
            "session_id": "s1",
            "root_ac_index": 0,
            "semantic_ac_key": "ac-key",
            "retry_attempt": 0,
            "configured_retry_attempts": 2,
            "retry_termination_reason": "not_retryable",
            "alternate_redispatch_status": "not_attempted",
            "last_failure_class": "unknown",
            "success": False,
        }
        closure_data.update(corruption)
        closure = BaseEvent(
            type="execution.ac.recovery_exhausted",
            aggregate_type="execution",
            aggregate_id="exec-original",
            data=closure_data,
        )
        event_store = AsyncMock()
        event_store.replay.return_value = [finalized, closure]
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            ac_retry_attempts=2,
        )

        recovered = await executor._reconstruct_finalized_outcomes(
            execution_id="exec-original",
            total_acs=1,
        )

        assert recovered is None

    @pytest.mark.asyncio
    async def test_valid_recovery_exhausted_payload_closes_latest_failure(self) -> None:
        from ouroboros.events.base import BaseEvent

        event_store = AsyncMock()
        event_store.replay.return_value = [
            BaseEvent(
                type="execution.ac.outcome_finalized",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "ac_index": 0,
                    "retry_attempt": 2,
                    "success": False,
                    "outcome": "failed",
                    "is_decomposed": False,
                    "forced_frontier_routing": False,
                    "context_summary": None,
                },
            ),
            BaseEvent(
                type="execution.ac.recovery_exhausted",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "schema_version": 1,
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "semantic_ac_key": "ac-key",
                    "retry_attempt": 2,
                    "configured_retry_attempts": 2,
                    "retry_termination_reason": "budget_exhausted",
                    "alternate_redispatch_status": "not_attempted",
                    "last_failure_class": "unknown",
                    "success": False,
                },
            ),
        ]
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            ac_retry_attempts=2,
        )

        recovered = await executor._reconstruct_finalized_outcomes(
            execution_id="exec-original",
            total_acs=1,
        )

        assert recovered is not None
        assert recovered[0].success is False
        assert recovered[0].recovery_exhausted is True

    @pytest.mark.asyncio
    async def test_recovery_exhausted_cannot_close_same_attempt_success(self) -> None:
        """A failure closure cannot overwrite an authoritative success marker."""
        from ouroboros.events.base import BaseEvent

        event_store = AsyncMock()
        event_store.replay.return_value = [
            BaseEvent(
                type="execution.ac.outcome_finalized",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "ac_index": 0,
                    "retry_attempt": 2,
                    "success": True,
                    "outcome": "succeeded",
                    "is_decomposed": False,
                    "forced_frontier_routing": False,
                    "context_summary": None,
                },
            ),
            BaseEvent(
                type="execution.ac.recovery_exhausted",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "schema_version": 1,
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "semantic_ac_key": "ac-key",
                    "retry_attempt": 2,
                    "configured_retry_attempts": 2,
                    "retry_termination_reason": "budget_exhausted",
                    "alternate_redispatch_status": "not_attempted",
                    "last_failure_class": "unknown",
                    "success": False,
                },
            ),
        ]
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            ac_retry_attempts=2,
        )

        recovered = await executor._reconstruct_finalized_outcomes(
            execution_id="exec-original",
            total_acs=1,
        )

        assert recovered is None

    @pytest.mark.asyncio
    async def test_orphan_recovery_exhausted_marker_fails_closed(self) -> None:
        """A closure without its exact finalized failure is not authoritative."""
        from ouroboros.events.base import BaseEvent

        event_store = AsyncMock()
        event_store.replay.return_value = [
            BaseEvent(
                type="execution.ac.recovery_exhausted",
                aggregate_type="execution",
                aggregate_id="exec-original",
                data={
                    "schema_version": 1,
                    "execution_id": "exec-original",
                    "session_id": "s1",
                    "root_ac_index": 0,
                    "semantic_ac_key": "ac-key",
                    "retry_attempt": 2,
                    "configured_retry_attempts": 2,
                    "retry_termination_reason": "budget_exhausted",
                    "alternate_redispatch_status": "not_attempted",
                    "last_failure_class": "unknown",
                    "success": False,
                },
            )
        ]
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            ac_retry_attempts=2,
        )

        recovered = await executor._reconstruct_finalized_outcomes(
            execution_id="exec-original",
            total_acs=1,
        )

        assert recovered is None

    @pytest.mark.asyncio
    async def test_retry_consumption_reconstructed_from_finalized_markers(
        self, tmp_path: Path
    ) -> None:
        """The review's exact scenario: an AC has already consumed SOME
        ordinary retries (attempts 0 and 1 durably finalized; counter at 2)
        when the process dies mid attempt 2, before the ladder was ever
        reached. Resume must re-enter at attempt 2 with only the REMAINING
        budget — never at 0 with a fresh one. The outer verify/retry layer
        is REAL here (it emits the real durable markers); only the
        innermost dispatch is stubbed."""
        seed = self._two_ac_seed()
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"

        # --- Run 1: AC0 completes level 1 (checkpoint saved), then AC1
        # fails attempt 0 and ordinary retry 1 (both finalized durably by
        # the real outer layer) and the process crashes MID attempt 2.
        run1_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run1_events.initialize()
        run1_ckpt = CheckpointStore(base_path=ckpt_path)
        run1_ckpt.initialize()
        run1 = _executor(event_store=run1_events, checkpoint_store=run1_ckpt, ac_retry_attempts=3)
        run1_attempts: list[int] = []

        async def _run1_dispatch(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_indices"])  # type: ignore[call-overload]
            counters = kwargs["ac_retry_attempts"]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Prepare data", success=True)]
            attempt = counters[1]  # type: ignore[index]
            run1_attempts.append(attempt)
            if attempt >= 2:
                raise RuntimeError("simulated process crash mid ordinary retry")
            return [
                ACExecutionResult(
                    ac_index=1,
                    ac_content="Apply migration",
                    success=False,
                    error=f"migration failed on attempt {attempt}",
                    retry_attempt=attempt,
                )
            ]

        run1._execute_ac_batch = AsyncMock(side_effect=_run1_dispatch)
        with pytest.raises(BaseException, match="simulated process crash|unhandled errors"):
            await run1.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=self._two_stage_plan(),
            )
        await run1_events.close()
        # Precondition: attempts 0 and 1 ran and finalized; 2 was in flight.
        assert run1_attempts == [0, 1, 2]

        # --- Run 2: crash-restart of the same seed. The resumed AC must
        # pick up at attempt 2 (re-running the interrupted, never-finalized
        # attempt) and have exactly ONE ordinary retry left (attempt 3).
        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            ac_retry_attempts=3,
        )
        run2_attempts: list[int] = []

        async def _run2_dispatch(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_indices"])  # type: ignore[call-overload]
            counters = kwargs["ac_retry_attempts"]
            assert batch == [1]
            attempt = counters[1]  # type: ignore[index]
            run2_attempts.append(attempt)
            return [
                ACExecutionResult(
                    ac_index=1,
                    ac_content="Apply migration",
                    success=False,
                    error=f"migration failed on attempt {attempt}",
                    retry_attempt=attempt,
                )
            ]

        run2._execute_ac_batch = AsyncMock(side_effect=_run2_dispatch)
        result = await run2.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=[],
            system_prompt="system",
            execution_plan=self._two_stage_plan(),
        )
        await run2_events.close()

        # Pre-fix: the counter reset to 0 and the AC burned a full fresh
        # budget (attempts 0, 1, 2, 3 — repeating non-idempotent work under
        # already-finalized attempt numbers). Post-fix: total consumption is
        # tracked across the crash — only attempts 2 and 3 remain.
        assert run2_attempts == [2, 3]
        by_index = {r.ac_index: r for r in result.results}
        # AC0 was genuinely completed: restored, never re-dispatched.
        assert by_index[0].success is True
        assert by_index[0].final_message == "[Restored from checkpoint]"
        assert by_index[1].success is False
        # Recovery rejoined the ORIGINAL run's aggregate (which is exactly
        # where the finalized-attempt markers were replayed from).
        assert result.execution_id == "exec-original"

    @pytest.mark.asyncio
    async def test_finalized_success_after_checkpoint_is_restored_without_redispatch(
        self, tmp_path: Path
    ) -> None:
        seed = self._two_ac_seed()
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"
        run1_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run1_events.initialize()
        run1 = _executor(
            event_store=run1_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        real_level_completed = run1._emit_level_completed

        async def _crash_before_level_two_checkpoint(**kwargs: object) -> None:
            await real_level_completed(**kwargs)  # type: ignore[arg-type]
            if kwargs.get("level") == 2:
                raise RuntimeError("crash after finalized success")

        run1._emit_level_completed = _crash_before_level_two_checkpoint  # type: ignore[method-assign]
        run1._execute_ac_batch = AsyncMock(
            side_effect=[
                [ACExecutionResult(ac_index=0, ac_content="Prepare data", success=True)],
                [ACExecutionResult(ac_index=1, ac_content="Apply migration", success=True)],
            ]
        )
        with pytest.raises(BaseException, match="finalized success|unhandled errors"):
            await run1.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=self._two_stage_plan(),
            )
        await run1_events.close()

        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()
        result = await run2.execute_parallel(
            seed,
            session_id="session-restart",
            execution_id="exec-restart",
            tools=[],
            system_prompt="system",
            execution_plan=self._two_stage_plan(),
        )
        await run2_events.close()

        run2._run_batch_with_verify_and_retry.assert_not_awaited()
        by_index = {item.ac_index: item for item in result.results}
        assert by_index[1].success is True
        assert by_index[1].final_message == "[Restored from checkpoint]"

    @pytest.mark.asyncio
    async def test_finalized_cap_failure_before_terminal_marker_is_not_redispatched(
        self, tmp_path: Path
    ) -> None:
        seed = self._two_ac_seed()
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"
        run1_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run1_events.initialize()
        run1 = _executor(
            event_store=run1_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            ac_retry_attempts=0,
            lateral_escalation_enabled=False,
        )

        async def _crash_before_terminal_marker(**kwargs: object) -> None:
            result = kwargs["result"]
            if isinstance(result, ACExecutionResult) and result.ac_index == 1:
                raise RuntimeError("crash before terminal failure marker")

        run1._emit_recovery_exhausted = AsyncMock(side_effect=_crash_before_terminal_marker)
        run1._execute_ac_batch = AsyncMock(
            side_effect=[
                [ACExecutionResult(ac_index=0, ac_content="Prepare data", success=True)],
                [
                    ACExecutionResult(
                        ac_index=1,
                        ac_content="Apply migration",
                        success=False,
                        error="migration failed",
                    )
                ],
            ]
        )
        with pytest.raises(BaseException, match="terminal failure marker|unhandled errors"):
            await run1.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=self._two_stage_plan(),
            )
        await run1_events.close()

        run2_events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await run2_events.initialize()
        run2 = _executor(
            event_store=run2_events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            ac_retry_attempts=0,
            lateral_escalation_enabled=False,
        )
        run2._execute_ac_batch = AsyncMock()
        result = await run2.execute_parallel(
            seed,
            session_id="session-restart",
            execution_id="exec-restart",
            tools=[],
            system_prompt="system",
            execution_plan=self._two_stage_plan(),
        )
        await run2_events.close()

        run2._execute_ac_batch.assert_not_awaited()
        by_index = {item.ac_index: item for item in result.results}
        assert by_index[1].success is False
        assert "Restored durably finalized failure" in (by_index[1].error or "")


class TestFinalizedSuccessContextRecovery:
    """A success recovered from events must retain its downstream handoff."""

    @staticmethod
    def _seed_and_plan() -> tuple[Seed, StagedExecutionPlan]:
        seed = Seed(
            goal="Recover finalized success context",
            constraints=(),
            acceptance_criteria=("Prepare", "Implement API", "Verify integration"),
            ontology_schema=OntologySchema(name="ContextRecovery", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )
        plan = StagedExecutionPlan(
            nodes=(
                ACNode(index=0, content="Prepare"),
                ACNode(index=1, content="Implement API", depends_on=(0,)),
                ACNode(index=2, content="Verify integration", depends_on=(1,)),
            ),
            stages=(
                ExecutionStage(index=0, ac_indices=(0,)),
                ExecutionStage(index=1, ac_indices=(1,), depends_on_stages=(0,)),
                ExecutionStage(index=2, ac_indices=(2,), depends_on_stages=(1,)),
            ),
        )
        return seed, plan

    async def _crash_after_second_success(
        self,
        tmp_path: Path,
        *,
        append_legacy_marker: bool,
    ) -> tuple[Seed, StagedExecutionPlan, Path, Path]:
        from ouroboros.events.base import BaseEvent
        from ouroboros.orchestrator.adapter import AgentMessage

        seed, plan = self._seed_and_plan()
        db_path = tmp_path / "events.db"
        ckpt_path = tmp_path / "checkpoints"
        modified_file = tmp_path / "service.py"
        modified_file.write_text("def durable_api() -> str:\n    return 'ok'\n")
        events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await events.initialize()
        run1 = _executor(
            event_store=events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            task_cwd=str(tmp_path),
        )
        real_level_completed = run1._emit_level_completed

        async def _crash_before_second_stage_checkpoint(**kwargs: object) -> None:
            await real_level_completed(**kwargs)  # type: ignore[arg-type]
            if kwargs.get("level") == 2:
                raise RuntimeError("crash after finalized success before context checkpoint")

        run1._emit_level_completed = _crash_before_second_stage_checkpoint  # type: ignore[method-assign]
        run1._execute_ac_batch = AsyncMock(
            side_effect=[
                [ACExecutionResult(ac_index=0, ac_content="Prepare", success=True)],
                [
                    ACExecutionResult(
                        ac_index=1,
                        ac_content="Implement API",
                        success=True,
                        messages=(
                            AgentMessage(
                                type="tool",
                                content="edited",
                                tool_name="Edit",
                                data={"tool_input": {"file_path": str(modified_file)}},
                            ),
                            AgentMessage(
                                type="tool",
                                content="tested",
                                tool_name="Bash",
                            ),
                        ),
                        final_message="Implemented durable_api and verified it.",
                    )
                ],
            ]
        )
        with pytest.raises(BaseException, match="context checkpoint|unhandled errors"):
            await run1.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=[],
                system_prompt="system",
                execution_plan=plan,
            )

        if append_legacy_marker:
            await events.append(
                BaseEvent(
                    type="execution.ac.outcome_finalized",
                    aggregate_type="execution",
                    aggregate_id="exec-original",
                    data={
                        "execution_id": "exec-original",
                        "session_id": "session-original",
                        "root_ac_index": 1,
                        "ac_index": 1,
                        "retry_attempt": 0,
                        "success": True,
                        "outcome": "succeeded",
                        "is_decomposed": False,
                        "forced_frontier_routing": False,
                        # Pre-context-summary event shape.
                    },
                )
            )
        await events.close()
        return seed, plan, db_path, ckpt_path

    @pytest.mark.asyncio
    async def test_recovered_success_context_is_injected_into_downstream_stage(
        self, tmp_path: Path
    ) -> None:
        seed, plan, db_path, ckpt_path = await self._crash_after_second_success(
            tmp_path,
            append_legacy_marker=False,
        )
        events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await events.initialize()
        run2 = _executor(
            event_store=events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            task_cwd=str(tmp_path),
        )
        captured: dict[str, object] = {}

        async def _finish_downstream(**kwargs: object) -> list[ACExecutionResult]:
            captured["level_contexts"] = kwargs["level_contexts"]
            return [
                ACExecutionResult(
                    ac_index=2,
                    ac_content="Verify integration",
                    success=True,
                )
            ]

        run2._run_batch_with_verify_and_retry = AsyncMock(side_effect=_finish_downstream)
        result = await run2.execute_parallel(
            seed,
            session_id="session-restarted",
            execution_id="exec-restarted",
            tools=[],
            system_prompt="system",
            execution_plan=plan,
        )
        await events.close()

        contexts = captured["level_contexts"]
        assert isinstance(contexts, list)
        summaries = [summary for context in contexts for summary in context.completed_acs]
        recovered = next(summary for summary in summaries if summary.ac_index == 1)
        assert recovered.tools_used == ("Bash", "Edit")
        assert recovered.files_modified == (str(tmp_path / "service.py"),)
        assert recovered.key_output == "Implemented durable_api and verified it."
        assert "def durable_api" in recovered.public_api
        assert result.all_succeeded
        assert result.execution_id == "exec-original"

    @pytest.mark.asyncio
    async def test_legacy_success_without_context_blocks_unfinished_downstream(
        self, tmp_path: Path
    ) -> None:
        seed, plan, db_path, ckpt_path = await self._crash_after_second_success(
            tmp_path,
            append_legacy_marker=True,
        )
        events = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await events.initialize()
        run2 = _executor(
            event_store=events,
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            task_cwd=str(tmp_path),
        )
        run2._run_batch_with_verify_and_retry = AsyncMock()

        with pytest.raises(CheckpointUnreadableError, match="lacks the AC context"):
            await run2.execute_parallel(
                seed,
                session_id="session-restarted",
                execution_id="exec-restarted",
                tools=[],
                system_prompt="system",
                execution_plan=plan,
            )
        await events.close()
        run2._run_batch_with_verify_and_retry.assert_not_awaited()


class TestRunnerAuditRecordsRestoredExecutionSettings:
    """Round-16 finding #4 (BLOCKING): RC3 recovery restores the original
    run's execution-semantic settings INSIDE the executor (rounds 9-15),
    and dispatch correctly uses them — but the RUNNER persisted its own
    pre-recovery ``effective_workers``/``max_decomposition_depth`` into
    the completion summary / verification report, so the durable audit
    record described settings that were never the ones executed. The
    executor must carry the EXECUTED values back through
    ``ParallelExecutionResult`` (the round-10 #3 restored-execution_id
    pattern) and the runner must record those."""

    @pytest.mark.asyncio
    async def test_completion_summary_reflects_restored_settings(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from ouroboros.core.types import Result
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
        from ouroboros.orchestrator.runner import OrchestratorRunner
        from ouroboros.orchestrator.session import SessionTracker

        seed = Seed(
            goal="Config-drift crash recovery audit",
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
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content="Parent work"),
                ACNode(index=1, content="Verify integration", depends_on=(0,)),
            ),
            execution_levels=((0,), (1,)),
        )
        ckpt_path = tmp_path / "checkpoints"

        # --- Original run: dispatched under workers=4 / depth=5, crashes
        # during level 2, leaving a checkpoint that persists those
        # execution semantics (rounds 12/15).
        store = CheckpointStore(base_path=ckpt_path)
        store.initialize()
        original = _executor(
            event_store=AsyncMock(),
            checkpoint_store=store,
            max_concurrent=4,
            max_decomposition_depth=5,
            task_cwd=str(tmp_path),
        )

        async def _crashing_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            if batch == [0]:
                return [ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
            raise RuntimeError("simulated process crash during level 2")

        original._run_batch_with_verify_and_retry = AsyncMock(side_effect=_crashing_stage_runner)
        with pytest.raises(BaseException, match="simulated process crash|unhandled errors"):
            await original.execute_parallel(
                seed,
                session_id="session-original",
                execution_id="exec-original",
                tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]).tools,
                system_prompt="system",
                execution_plan=plan,
                system_prompt_builder=lambda **_kwargs: "system",
            )
        saved = CheckpointStore(base_path=ckpt_path).load(seed.metadata.seed_id)
        assert saved.is_ok
        assert saved.value.state["execution_semantics"]["max_concurrent"] == 4
        assert saved.value.state["execution_semantics"]["max_decomposition_depth"] == 5

        # --- Restarted process: the RUNNER is now configured DIFFERENTLY
        # (workers=1, depth=2). The executor's recovery restores and
        # dispatches under the ORIGINAL settings; the runner's durable
        # audit records must describe those, not its own config.
        mock_adapter = MagicMock(working_directory=str(tmp_path), runtime_backend="claude")
        mock_events = AsyncMock()
        mock_events.replay = AsyncMock(return_value=[])
        runner = OrchestratorRunner(
            mock_adapter,
            mock_events,
            MagicMock(),
            checkpoint_store=CheckpointStore(base_path=ckpt_path),
            max_parallel_workers=1,
            max_decomposition_depth=2,
        )
        tracker = SessionTracker.create("exec-restarted", seed.metadata.seed_id)
        dispatched: list[list[int]] = []

        async def _resumed_stage_runner(**kwargs: object) -> list[ACExecutionResult]:
            batch = list(kwargs["batch_executable"])  # type: ignore[call-overload]
            dispatched.append(batch)
            return [
                ACExecutionResult(
                    ac_index=idx,
                    ac_content=str(seed.acceptance_criteria[idx]),
                    success=True,
                )
                for idx in batch
            ]

        class _RecoveringExecutor(ParallelACExecutor):
            def __init__(self, **kwargs: object) -> None:
                super().__init__(**kwargs)  # type: ignore[arg-type]
                self._run_batch_with_verify_and_retry = AsyncMock(  # type: ignore[method-assign]
                    side_effect=_resumed_stage_runner
                )

        mock_mark_completed = AsyncMock(return_value=Result.ok(None))
        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                _RecoveringExecutor,
            ),
        ):
            result = await runner._execute_parallel(
                seed=seed,
                exec_id="exec-restarted",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok
        # The recovery genuinely resumed: only the unfinished AC dispatched.
        assert dispatched == [[1]]
        summary = mock_mark_completed.await_args.args[1]
        # The durable audit record describes the settings that EXECUTED —
        # the checkpoint-restored originals, not this process's
        # pre-recovery configuration.
        assert summary["effective_parallel_workers"] == 4
        assert summary["max_decomposition_depth"] == 5
        # The requested (current-process) worker config remains documented
        # as the request it is.
        assert summary["max_parallel_workers"] == 1
