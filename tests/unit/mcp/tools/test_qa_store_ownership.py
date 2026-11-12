"""Consumer QA must be observable in the consumer's SQLite store."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.evolution.loop import EvolutionaryLoop
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler
from ouroboros.mcp.tools.query_handlers import SessionStatusHandler
from ouroboros.persistence.event_store import EventStore, sqlite_database_url
from ouroboros.persistence.qa_session_store import QA_EVALUATED
from ouroboros.providers.base import CompletionResponse, UsageInfo


@pytest.mark.parametrize("inject_handler_store", [True, False])
@pytest.mark.parametrize("blocked_default", [True, False])
async def test_evolve_qa_uses_loop_store_and_survives_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inject_handler_store, blocked_default
):
    """Run the public consumer and real evolution; replace only inference boundaries."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    # No checks or detector inference are needed for this synthetic artifact.
    (project / ".ouroboros").mkdir()
    (project / ".ouroboros" / "mechanical.toml").write_text("[commands]\n", encoding="utf-8")
    default_dir = Path.home() / ".ouroboros"
    if blocked_default:
        default_dir.write_text("not a directory", encoding="utf-8")

    seed = Seed(
        goal="Return a synthetic artifact",
        constraints=(),
        acceptance_criteria=("An artifact is returned",),
        ontology_schema=OntologySchema(name="Artifact", description="Synthetic", fields=()),
        metadata=SeedMetadata(seed_id="seed-custom-db", ambiguity_score=0.1),
    )

    async def executor(*args, **kwargs):
        return "Synthetic artifact"

    async def evaluator(*args, **kwargs):
        return Result.ok(
            EvaluationSummary(
                final_approved=True,
                highest_stage_passed=2,
                score=0.9,
                ac_results=(
                    ACResult(ac_index=0, ac_content="An artifact is returned", passed=True),
                ),
            )
        )

    inference = SimpleNamespace(complete=AsyncMock())
    inference.complete.return_value = Result.ok(
        CompletionResponse(content='{"score": 0.9}', model="fake", usage=UsageInfo(0, 0, 0))
    )
    monkeypatch.setattr("ouroboros.mcp.tools.qa.create_llm_adapter", lambda **_kw: inference)
    monkeypatch.setattr("ouroboros.providers.factory.create_llm_adapter", lambda **_kw: inference)
    monkeypatch.setattr("ouroboros.providers.create_llm_adapter", lambda **_kw: inference)
    url = sqlite_database_url(tmp_path / "custom.sqlite")
    store = EventStore(url)
    await store.initialize()
    loop = EvolutionaryLoop(event_store=store, executor=executor, evaluator=evaluator)
    server = MCPServerAdapter()
    server.register_tool(
        EvolveStepHandler(
            evolutionary_loop=loop, event_store=store if inject_handler_store else None
        )
    )
    server.register_tool(SessionStatusHandler(event_store=store))
    try:
        result = await server.call_tool(
            "ouroboros_evolve_step",
            {"lineage_id": "lin-custom-db", "seed_content": json.dumps(seed.to_dict())},
        )
        assert result.is_ok, result.error if result.is_err else result
        assert result.value.meta["qa_attempted"]
        assert "qa" in result.value.meta, result.value.text_content
        session_id = result.value.meta["qa"]["qa_session_id"]
        assert f"Session: {session_id}" in result.value.text_content
        status = await server.call_tool("ouroboros_session_status", {"session_id": session_id})
        assert status.is_ok, str(status.error) if status.is_err else status
        assert status.value.meta["score"] == 0.9
        assert status.value.meta["session_kind"] == "qa"
        events = await store.query_events(event_type=QA_EVALUATED)
        assert len(events) == 1
        assert events[0].aggregate_id == session_id
    finally:
        await store.close()

    # The caller's close must be enough; QA must not keep another connection alive.
    reopened = EventStore(url)
    try:
        status = await SessionStatusHandler(event_store=reopened).handle({"session_id": session_id})
        assert status.is_ok
        assert status.value.meta["status"] == "completed"
        assert status.value.meta["passed"] is True
    finally:
        await reopened.close()
    assert not (default_dir / "ouroboros.db").exists()
    assert inference.complete.await_count >= 1
