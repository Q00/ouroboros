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


async def _submit_start_turn(
    submit: SubmitFanoutResultsHandler,
    meta: dict[str, Any],
    session_id: str,
    *,
    ran: frozenset[str] = frozenset({"code_context", "web_context"}),
) -> str:
    """Submit the lanes in ``ran`` of an attached start turn; mark the rest undispatched."""
    results: list[dict[str, Any]] = []
    for payload in meta["question_advisory_subagents"]:
        lane_id = payload["context"]["lane_id"]
        if lane_id in ran:
            results.append({"key": lane_id, "content": f"{lane_id} finding"})
        else:
            results.append({"key": lane_id, "undispatched": True})
    result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": meta["question_advisory_fanout_id"],
            "correlation_key": "context.lane_id",
            "results": results,
        }
    )
    assert result.is_ok, result
    return result.unwrap().meta["contract_id"]


def _lane_prompt(meta: dict[str, Any], lane_id: str) -> str:
    return next(
        payload["prompt"]
        for payload in meta["question_advisory_subagents"]
        if payload["context"]["lane_id"] == lane_id
    )


@pytest.mark.asyncio
async def test_attached_start_turn_is_offered_to_later_web_lane(tmp_path: Any) -> None:
    from ouroboros.mcp.tools.question_advisory import attach_question_advisory

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore.for_project(workspace)
    store.initialize()
    registry = FanoutRegistry(tmp_path / "fanout")
    submit = SubmitFanoutResultsHandler(
        fanout_registry=registry, disposable_memory=DisposableMemory(artifact_store=store)
    )
    session_id = "interview-attached"

    def attach(phase: str, question: str) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        attach_question_advisory(
            meta,
            tool_name="ouroboros_interview",
            session_id=session_id,
            question=question,
            phase=phase,
            fanout_registry=registry,
            findings_store=store,
        )
        return meta

    start = attach("start", "Which payment provider does checkout use?")
    assert "web_context" not in start["question_advisory_request"].get("recent_findings", {})
    contract_id = await _submit_start_turn(submit, start, session_id)
    assert store.fetch(contract_id).body["provenance"]["phase"] == "start"

    answer = attach("answer", "What refund window should checkout allow?")
    offered = answer["question_advisory_request"]["recent_findings"]
    assert offered["web_context"] == [
        interview_baseline_by_lane(store, session_id=session_id)["web_context"]
    ]
    assert [entry["contract_id"] for entry in offered["code_context"]] == [contract_id]
    assert contract_id in _lane_prompt(answer, "web_context")
    assert contract_id not in _lane_prompt(answer, "ambiguity_contrarian")


def test_baseline_entry_is_offered_once_and_survives_a_full_window() -> None:
    from ouroboros.mcp.tools.recent_findings import with_interview_baseline

    def entry(contract_id: str, lane_id: str) -> dict[str, Any]:
        return {"contract_id": contract_id, "lane_id": lane_id, "published_at": "t"}

    full = {"code_context": [entry(f"newer-{index}", "code_context") for index in range(20)]}
    baseline = {
        "code_context": entry("start", "code_context"),
        "web_context": entry("start", "web_context"),
    }

    merged = with_interview_baseline(full, baseline, lanes={"code_context", "web_context"})

    assert len(merged["code_context"]) == 20
    assert merged["code_context"][-1]["contract_id"] == "start"
    assert "newer-19" not in {item["contract_id"] for item in merged["code_context"]}
    assert merged["web_context"] == [baseline["web_context"]]
    again = with_interview_baseline(merged, baseline, lanes={"code_context", "web_context"})
    assert [item["contract_id"] for item in again["code_context"]].count("start") == 1
    assert with_interview_baseline({}, baseline, lanes={"code_context"}) == {
        "code_context": [baseline["code_context"]]
    }


@pytest.mark.asyncio
async def test_undispatched_start_web_lane_is_not_offered_later(tmp_path: Any) -> None:
    from ouroboros.mcp.tools.question_advisory import attach_question_advisory

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore.for_project(workspace)
    store.initialize()
    registry = FanoutRegistry(tmp_path / "fanout")
    submit = SubmitFanoutResultsHandler(
        fanout_registry=registry, disposable_memory=DisposableMemory(artifact_store=store)
    )
    session_id = "interview-undispatched-web"

    def attach(phase: str, question: str) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        attach_question_advisory(
            meta,
            tool_name="ouroboros_interview",
            session_id=session_id,
            question=question,
            phase=phase,
            fanout_registry=registry,
            findings_store=store,
        )
        return meta

    start = attach("start", "Which payment provider does checkout use?")
    await _submit_start_turn(submit, start, session_id, ran=frozenset({"code_context"}))

    answer = attach("answer", "What refund window should checkout allow?")
    assert "web_context" not in answer["question_advisory_request"]["recent_findings"]
