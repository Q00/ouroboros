"""Foundational provenance and lookup for reusable interview artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
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


def _current_verified_at() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    verified_at = _current_verified_at()
    web = {
        "question_identity": question_identity,
        "lane_id": "web_context",
        "status": "references_found",
        "search_queries": ["subscription billing official guidance"],
        "references": [
            {
                "title": "Stripe Billing documentation",
                "url": "https://docs.stripe.com/billing",
                "source_type": "official",
                "relevance": "Primary billing lifecycle reference.",
                "verified_at": verified_at,
            },
            {
                "title": "W3C Web Payments",
                "url": "https://www.w3.org/Payments/WG/",
                "source_type": "standard",
                "relevance": "Standards context for web payments.",
                "verified_at": verified_at,
            },
        ],
    }
    source_evidence = {
        "attested_by": "parent_runtime",
        "search_queries": list(web["search_queries"]),
        "search_attempts": [
            {
                "query": query,
                "outcome": "results_found",
                "result_urls": [reference["url"] for reference in web["references"]],
            }
            for query in web["search_queries"]
        ],
        "fetched_sources": [
            {
                "url": reference["url"],
                "http_status": 200,
                "source_type": reference["source_type"],
                "verified_at": reference["verified_at"],
            }
            for reference in web["references"]
        ],
    }

    result = await submit.handle(
        {
            "session_id": session_id,
            "fanout_id": fanout_id,
            "correlation_key": "context.lane_id",
            "results": [
                {"key": "code_context", "content": {"claim": "local fact"}},
                {
                    "key": "web_context",
                    "content": web,
                    "source_evidence": source_evidence,
                },
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
    assert body["source_evidence"] == {"web_context": source_evidence}
    baseline = interview_baseline_by_lane(store, session_id=session_id)
    assert set(baseline) == {"code_context", "web_context"}
    assert {entry["contract_id"] for entry in baseline.values()} == {contract_id}
    assert interview_baseline_by_lane(store, session_id="another-session") == {}


@pytest.mark.asyncio
async def test_answer_repair_joins_start_snapshot_and_is_reusable(tmp_path: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore.for_project(workspace)
    store.initialize()
    registry = FanoutRegistry(tmp_path / "fanout")
    memory = DisposableMemory(artifact_store=store)
    submit = SubmitFanoutResultsHandler(fanout_registry=registry, disposable_memory=memory)
    session_id = "interview-foundation"
    question_identity = "interview-question:fedcba9876543210"

    async def publish(lane_id: str, phase: str) -> str:
        fanout_id = register_question_advisory_fanout(
            registry,
            session_id=session_id,
            payloads=[_payload(lane_id, question_identity=question_identity)],
            phase=phase,
        )
        assert fanout_id is not None
        result_entry: dict[str, Any] = {
            "key": lane_id,
            "content": {"claim": f"{phase} fact"},
        }
        if lane_id == "web_context":
            url = "https://example.com/reference"
            verified_at = _current_verified_at()
            query = "official reference"
            result_entry["content"] = {
                "question_identity": question_identity,
                "lane_id": "web_context",
                "status": "references_found",
                "search_queries": [query],
                "references": [
                    {
                        "title": "Official reference",
                        "url": url,
                        "source_type": "official",
                        "relevance": "Defines the relevant behavior.",
                        "verified_at": verified_at,
                    },
                    {
                        "title": "Supporting reference",
                        "url": "https://example.org/reference",
                        "source_type": "reputable_secondary",
                        "relevance": "Corroborates the official reference.",
                        "verified_at": verified_at,
                    },
                ],
            }
            result_entry["source_evidence"] = {
                "attested_by": "parent_runtime",
                "search_queries": [query],
                "search_attempts": [
                    {
                        "query": query,
                        "outcome": "results_found",
                        "result_urls": [
                            reference["url"] for reference in result_entry["content"]["references"]
                        ],
                    }
                    for query in [query]
                ],
                "fetched_sources": [
                    {
                        "url": reference["url"],
                        "http_status": 200,
                        "source_type": reference["source_type"],
                        "verified_at": reference["verified_at"],
                    }
                    for reference in result_entry["content"]["references"]
                ],
            }
        result = await submit.handle(
            {
                "session_id": session_id,
                "fanout_id": fanout_id,
                "correlation_key": "context.lane_id",
                "results": [result_entry],
            }
        )
        assert result.is_ok, result
        return str(result.unwrap().meta["contract_id"])

    start_contract = await publish("code_context", "start")
    repair_contract = await publish("web_context", "resume_pending")
    baseline = interview_baseline_by_lane(store, session_id=session_id)

    assert baseline["code_context"]["contract_id"] == start_contract
    assert baseline["web_context"]["contract_id"] == repair_contract
    assert set(baseline) == {"code_context", "web_context"}


@pytest.mark.asyncio
async def test_unrelated_phase_is_not_reusable_baseline(tmp_path: Any) -> None:
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
        phase="completion_gate",
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
