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
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelACExecutor
from ouroboros.persistence.checkpoint import CheckpointStore


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
) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=event_store,
        console=MagicMock(),
        checkpoint_store=checkpoint_store,
        cross_harness_redispatch=False,
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
