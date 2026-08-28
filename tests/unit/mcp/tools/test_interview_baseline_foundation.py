"""Foundational provenance and lookup for reusable interview artifacts."""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.mcp.tools.evaluation_handlers import SubmitFanoutResultsHandler
from ouroboros.mcp.tools.fanout import FanoutRegistry, register_question_advisory_fanout
from ouroboros.mcp.tools.recent_findings import interview_baseline_by_lane
from ouroboros.mcp.tools.subagent import build_subagent_payload
from ouroboros.orchestrator.disposable_memory import DisposableMemory
from ouroboros.persistence.artifact_store import ArtifactStore


def _payload(lane_id: str, *, question_identity: str):
    return build_subagent_payload(
        tool_name="ouroboros_interview",
        title=f"baseline:{lane_id}",
        prompt="inspect",
        agent="researcher",
        context={
            "lane_id": lane_id,
            "question_identity": question_identity,
            "required": False,
        },
    )


@pytest.mark.asyncio
async def test_start_fanout_publishes_server_provenance_and_is_reusable(tmp_path: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore.for_project(workspace)
    store.initialize()
    registry = FanoutRegistry(tmp_path / "fanout")
    memory = DisposableMemory(artifact_store=store)
    submit = SubmitFanoutResultsHandler(fanout_registry=registry, disposable_memory=memory)
    session_id = "interview-foundation"
    question_identity = "interview-question:0123456789abcdef"
    payloads = [
        _payload("code_context", question_identity=question_identity),
        _payload("web_context", question_identity=question_identity),
    ]
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id=session_id,
        payloads=payloads,
        phase="start",
    )
    assert fanout_id is not None

    result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": fanout_id,
            "correlation_key": "context.lane_id",
            "results": [
                {"key": "code_context", "content": {"claim": "local fact"}},
                {"key": "web_context", "content": {"claim": "external fact"}},
            ],
        }
    )

    assert result.is_ok, result
    contract_id = result.unwrap().meta["contract_id"]
    body = store.fetch(contract_id).body
    assert body["provenance"] == {
        "session_id": session_id,
        "phase": "start",
        "question_identity": question_identity,
    }
    baseline = interview_baseline_by_lane(store, session_id=session_id)
    assert set(baseline) == {"code_context", "web_context"}
    assert {entry["contract_id"] for entry in baseline.values()} == {contract_id}
    assert interview_baseline_by_lane(store, session_id="another-session") == {}


@pytest.mark.asyncio
async def test_non_start_fanout_is_not_reusable_baseline(tmp_path: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore.for_project(workspace)
    store.initialize()
    registry = FanoutRegistry(tmp_path / "fanout")
    memory = DisposableMemory(artifact_store=store)
    submit = SubmitFanoutResultsHandler(fanout_registry=registry, disposable_memory=memory)
    session_id = "interview-foundation"
    question_identity = "interview-question:fedcba9876543210"
    payloads = [_payload("code_context", question_identity=question_identity)]
    fanout_id = register_question_advisory_fanout(
        registry,
        session_id=session_id,
        payloads=payloads,
        phase="answer",
    )
    assert fanout_id is not None

    result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": fanout_id,
            "correlation_key": "context.lane_id",
            "results": [{"key": "code_context", "content": {"claim": "later fact"}}],
        }
    )

    assert result.is_ok, result
    assert interview_baseline_by_lane(store, session_id=session_id) == {}
