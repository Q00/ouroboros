"""Fixture-only execution proof for the Full host adapter seam.

This is not production ChatGPT authority and must not be used to claim AC-03.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ouroboros.mcp.server.adapter import create_ouroboros_server
from ouroboros.mcp.tools.host_bridge import HostDispatchContext
from ouroboros.persistence.event_store import EventStore

SEED = """\
goal: Write the requested artifact
constraints: []
acceptance_criteria:
  - The artifact exists
orchestrator:
  execution_mode: legacy
ontology_schema:
  name: HostFixture
  description: Host fixture
  fields:
    - name: artifact
      field_type: string
      description: Artifact path
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: host-fixture
  version: "1.0.0"
  created_at: "2026-07-14T00:00:00Z"
  ambiguity_score: 0.0
  interview_id: null
"""


def test_full_composition_uses_one_host_llm_adapter_family(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'full-composition.db'}")
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )

    server = create_ouroboros_server(
        runtime_backend="codex",
        event_store=store,
        host_dispatch_context=context,
    )
    auto = server._tool_handlers["ouroboros_auto"]
    evaluate = server._tool_handlers["ouroboros_evaluate"]

    assert auto.interview_handler.llm_adapter is auto.generate_seed_handler.llm_adapter
    assert evaluate.llm_adapter is auto.interview_handler.llm_adapter
    assert context.authority_source.value == "fixture"


def test_full_composition_forwards_one_host_context_to_every_loop_stage(
    tmp_path: Path,
) -> None:
    """The composition root must not drop host authority between Full stages."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'full-stage-context.db'}")
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )

    server = create_ouroboros_server(
        runtime_backend="codex",
        event_store=store,
        host_dispatch_context=context,
    )

    for tool_name in (
        "ouroboros_auto",
        "ouroboros_start_auto",
        "ouroboros_evaluate",
        "ouroboros_start_evaluate",
        "ouroboros_evolve_step",
        "ouroboros_start_evolve_step",
        "ouroboros_ralph",
        "ouroboros_start_ralph",
    ):
        handler = server._tool_handlers[tool_name]
        stage_handler = getattr(handler, "_evaluate_handler", handler)
        assert stage_handler.host_dispatch_context is context, tool_name


@pytest.mark.asyncio
async def test_execute_seed_pauses_for_host_and_resumes_same_full_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Full execute handler pauses, accepts host evidence, and resumes."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-driven execution must not construct a CLI/model runtime")

    monkeypatch.setattr("ouroboros.orchestrator.create_agent_runtime", forbidden)
    monkeypatch.setattr("ouroboros.providers.create_llm_adapter", forbidden)
    monkeypatch.setattr("ouroboros.mcp.tools.execution_handlers.create_agent_runtime", forbidden)
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'full-loop.db'}")
    await store.initialize()
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )
    server = create_ouroboros_server(
        runtime_backend="codex",
        event_store=store,
        host_dispatch_context=context,
    )
    arguments = {
        "seed_content": SEED,
        "cwd": str(tmp_path),
        "use_worktree": False,
        "skip_qa": True,
    }

    pending = await server.call_tool("ouroboros_execute_seed", arguments)
    assert pending.is_ok
    assert pending.value.meta["status"] == "host_work_pending"
    order = pending.value.meta["work_order"]
    assert order["session_id"] == order["lineage_id"]
    pending_status = await server.call_tool(
        "ouroboros_session_status", {"session_id": order["session_id"]}
    )
    assert pending_status.is_ok
    assert pending_status.value.meta["status"] == "paused"

    receipt = {
        "dispatch_id": order["dispatch_id"],
        "session_id": order["session_id"],
        "lineage_id": order["lineage_id"],
        "workspace_id": order["workspace_id"],
        "workspace_root": order["workspace_root"],
        "sandbox_mode": order["sandbox_mode"],
        "approval_policy": order["approval_policy"],
        "terminal_status": "completed",
        "criterion_results": (
            {
                "criterion": "The artifact exists",
                "passed": True,
                "evidence_refs": ("fixture:test",),
            },
        ),
        "evidence": ({"kind": "test", "value": "fixture:test"},),
        "changed_paths": (),
        "completed_at": datetime.now(UTC).isoformat(),
        "receipt_sha256": "c" * 64,
    }
    completed = await server.call_tool("ouroboros_complete_host_dispatch", {"receipt": receipt})
    assert completed.is_ok

    continuation = order["context"]["continuation"]
    resumed = await server.call_tool(continuation["tool_name"], continuation["arguments"])
    assert resumed.is_ok
    assert resumed.value.meta["session_id"] == order["session_id"]
    assert resumed.value.meta["status"] == "completed"
    completed_status = await server.call_tool(
        "ouroboros_session_status", {"session_id": order["session_id"]}
    )
    assert completed_status.is_ok
    assert completed_status.value.meta["status"] == "completed"

    host_events = await store.replay("host_dispatch", order["lineage_id"])
    assert [event.type for event in host_events] == [
        "host.dispatch.requested",
        "host.dispatch.accepted",
        "execution.started",
        "status.projected",
        "evidence.recorded",
        "execution.completed",
    ]
    assert {event.aggregate_id for event in host_events} == {order["lineage_id"]}
    await store.close()


@pytest.mark.asyncio
async def test_chatgpt_host_profile_never_falls_through_to_configured_cli_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host authority wins even when a stale config names a sequential backend."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ChatGPT host execution must not construct a CLI/model runtime")

    monkeypatch.setattr("ouroboros.orchestrator.create_agent_runtime", forbidden)
    monkeypatch.setattr("ouroboros.providers.create_llm_adapter", forbidden)
    monkeypatch.setattr("ouroboros.mcp.tools.execution_handlers.create_agent_runtime", forbidden)
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )
    server = create_ouroboros_server(
        runtime_backend="gemini",
        event_store=EventStore(f"sqlite+aiosqlite:///{tmp_path / 'no-fallback.db'}"),
        host_dispatch_context=context,
    )

    result = await server.call_tool(
        "ouroboros_execute_seed",
        {"seed_content": SEED, "cwd": str(tmp_path), "use_worktree": False},
    )

    assert result.is_ok
    assert result.value.meta["status"] == "host_work_pending"


@pytest.mark.asyncio
async def test_chatgpt_host_profile_rejects_cwd_outside_host_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=workspace,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )
    server = create_ouroboros_server(
        runtime_backend="codex",
        event_store=EventStore(f"sqlite+aiosqlite:///{tmp_path / 'workspace-boundary.db'}"),
        host_dispatch_context=context,
    )

    result = await server.call_tool(
        "ouroboros_execute_seed",
        {"seed_content": SEED, "cwd": str(outside), "use_worktree": False},
    )

    assert result.is_err
    assert "active ChatGPT workspace" in str(result.error)
