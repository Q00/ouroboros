"""Embedded callers publish real QA to their owner, leaving the default DB alone.

Auto is driven at its existing pipeline/evaluator boundary; run is driven at
its runner execution boundary. Interviews, execution and providers are synthetic.
QAHandler, its adapters, publication, and reopened status queries are real.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.cli.commands.auto import _run_auto
from ouroboros.cli.commands.run import _run_orchestrator
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.evaluation.verification_artifacts import VerificationArtifacts
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.auto_handler import AutoHandler
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.mcp.tools.query_handlers import SessionStatusHandler
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.persistence.event_store import EventStore, sqlite_database_url
from ouroboros.persistence.qa_session_store import QA_EVALUATED
from ouroboros.providers.base import CompletionResponse, UsageInfo


@pytest.fixture
async def ownership(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off")
    seed = Seed(
        goal="Return a synthetic artifact",
        constraints=(),
        acceptance_criteria=("An artifact is returned",),
        ontology_schema=OntologySchema(name="Artifact", description="Synthetic", fields=()),
        metadata=SeedMetadata(seed_id="seed-owned-qa", ambiguity_score=0.1),
    )
    (Path.home() / ".ouroboros").mkdir(parents=True, exist_ok=True)
    default = EventStore(sqlite_database_url(Path.home() / ".ouroboros" / "ouroboros.db"))
    custom = EventStore(sqlite_database_url(tmp_path / "custom.sqlite"))
    await default.initialize()
    await custom.initialize()
    sentinel = BaseEvent(type="test.sentinel", aggregate_type="test", aggregate_id="default")
    await default.append(sentinel)
    inference = SimpleNamespace(
        complete=AsyncMock(
            return_value=Result.ok(
                CompletionResponse(content='{"score": 0.9}', model="fake", usage=UsageInfo(0, 0, 0))
            )
        )
    )
    monkeypatch.setattr("ouroboros.mcp.tools.qa.create_llm_adapter", lambda **_kw: inference)
    try:
        yield seed, custom, default, sentinel, inference
    finally:
        await custom.close()
        await default.close()


async def assert_owned_verdict(custom, default, sentinel):
    events = await custom.query_events(event_type=QA_EVALUATED)
    default_events = await default.query_events()
    assert [e.id for e in default_events] == [sentinel.id], default_events
    assert len(events) == 1, events
    session_id = events[0].aggregate_id
    status = await SessionStatusHandler(event_store=custom).handle({"session_id": session_id})
    assert status.is_ok
    assert status.value.meta["score"] == 0.9
    assert status.value.meta["passed"] is True
    await custom.close()
    reopened = EventStore(custom.database_url)
    try:
        status = await SessionStatusHandler(event_store=reopened).handle({"session_id": session_id})
        assert status.is_ok
        assert status.value.meta["status"] == "completed"
        assert status.value.meta["score"] == 0.9
    finally:
        await reopened.close()


@pytest.mark.parametrize("caller", ["mcp_auto", "cli_auto"])
async def test_auto_caller_owns_seed_qa(caller, ownership, monkeypatch, tmp_path):
    seed, custom, default, sentinel, inference = ownership

    async def grade_seed(pipeline, state):
        # Keep the caller's actual evaluator and QA; bypass unrelated interview
        # and repair work at the same stable seam as other auto caller tests.
        evaluated = await pipeline.seed_qa_evaluator(seed, SeedDraftLedger.from_goal(state.goal))
        assert evaluated.passed
        assert evaluated.score == 0.9
        return AutoPipelineResult(
            status="complete", auto_session_id=state.auto_session_id, phase="complete"
        )

    monkeypatch.setattr("ouroboros.auto.pipeline.AutoPipeline.run", grade_seed)
    if caller == "mcp_auto":
        result = await AutoHandler(event_store=custom, agent_runtime_backend="codex").handle(
            {"goal": seed.goal, "cwd": str(tmp_path), "skip_run": True, "worktree_policy": "none"}
        )
        assert result.is_ok, result
    else:
        monkeypatch.setattr("ouroboros.cli.commands.auto.EventStore", lambda: custom)
        result = await _run_auto(
            goal=seed.goal,
            resume=None,
            runtime="codex",
            max_interview_rounds=1,
            max_repair_rounds=1,
            skip_run=True,
            worktree_policy="none",
        )
        assert result.status == "complete"
        # CLI owns and closes this store; reinitialize for the public observer.
        await custom.initialize()
    await assert_owned_verdict(custom, default, sentinel)
    assert inference.complete.await_count == 1


@pytest.mark.parametrize("caller", ["execute_seed", "cli_run"])
async def test_execution_caller_owns_post_run_qa(caller, ownership, monkeypatch, tmp_path):
    seed, custom, default, sentinel, inference = ownership

    async def prepare(runner, seed, *, execution_id=None, session_id=None, **_kw):
        return await runner._session_repo.create_session(
            execution_id=execution_id or "exec-owned",
            session_id=session_id,
            seed_id=seed.metadata.seed_id,
        )

    async def execute(runner, *, tracker, **_kw):
        completed = await runner._session_repo.mark_completed(tracker.session_id)
        assert completed.is_ok, completed
        return Result.ok(
            SimpleNamespace(
                success=True,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                summary={},
                final_message="Synthetic artifact",
                messages_processed=1,
                duration_seconds=0.01,
            )
        )

    async def execute_seed(runner, *, seed, **kwargs):
        prepared = await prepare(runner, seed, **kwargs)
        assert prepared.is_ok, prepared
        return await execute(runner, tracker=prepared.value)

    verification = VerificationArtifacts(
        artifact="Synthetic artifact",
        reference="Synthetic reference",
        artifact_dir=str(tmp_path),
        manifest_path=str(tmp_path / "manifest.json"),
    )
    # Real runner construction + real session repository, synthetic execution.
    monkeypatch.setattr(OrchestratorRunner, "prepare_session", prepare)
    monkeypatch.setattr(OrchestratorRunner, "execute_precreated_session", execute)
    monkeypatch.setattr(OrchestratorRunner, "execute_seed", execute_seed)

    def runtime(**_kw):
        return SimpleNamespace(runtime_backend="codex", working_directory=str(tmp_path))

    monkeypatch.setattr("ouroboros.orchestrator.create_agent_runtime", runtime)
    monkeypatch.setattr("ouroboros.mcp.tools.execution_handlers.create_agent_runtime", runtime)
    for module in ("ouroboros.cli.commands.run", "ouroboros.mcp.tools.execution_handlers"):
        monkeypatch.setattr(
            f"{module}.build_verification_artifacts", AsyncMock(return_value=verification)
        )
    monkeypatch.setattr(
        "ouroboros.cli.commands.run_successors.continue_run_into_evaluation", AsyncMock()
    )
    if caller == "execute_seed":
        result = await ExecuteSeedHandler(event_store=custom, agent_runtime_backend="codex").handle(
            {
                "seed_content": yaml.safe_dump(seed.to_dict()),
                "cwd": str(tmp_path),
                "use_worktree": False,
            },
            synchronous=True,
        )
        assert result.is_ok, result
        assert result.value.meta["status"] == "completed", result.value
    else:
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text(yaml.safe_dump(seed.to_dict()), encoding="utf-8")
        # _run_orchestrator imports this dependency locally. QA's default-store
        # reference must stay bound to the real class even when its module is
        # imported lazily; omission then writes to the forbidden default DB.
        monkeypatch.setattr("ouroboros.mcp.tools.qa_session.EventStore", EventStore)
        monkeypatch.setattr("ouroboros.persistence.event_store.EventStore", lambda: custom)
        await _run_orchestrator(seed_file, runtime_backend="codex", check_package=False)
    await assert_owned_verdict(custom, default, sentinel)
    assert inference.complete.await_count == 1
