"""ChatGPT-hosted Auto uses Full state without nested runtimes or jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros.auto.state import AutoPhase, AutoStore
from ouroboros.mcp.tools.auto_handler import AutoHandler, StartAutoHandler
from ouroboros.mcp.tools.host_bridge import (
    HostCompletionReceipt,
    HostDispatchContext,
    HostStageBridge,
)
from ouroboros.persistence.event_store import EventStore


def _host_context(workspace: Path) -> HostDispatchContext:
    return HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=workspace,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )


def _receipt(order: dict[str, object]) -> HostCompletionReceipt:
    criterion = "Complete the Full Auto pipeline for the persisted auto session"
    return HostCompletionReceipt(
        dispatch_id=str(order["dispatch_id"]),
        session_id=str(order["session_id"]),
        lineage_id=str(order["lineage_id"]),
        workspace_id=str(order["workspace_id"]),
        workspace_root=Path(str(order["workspace_root"])),
        sandbox_mode=str(order["sandbox_mode"]),
        approval_policy=str(order["approval_policy"]),
        terminal_status="completed",
        criterion_results=(
            {
                "criterion": criterion,
                "passed": True,
                "evidence_refs": ("auto_result:fixture",),
            },
        ),
        evidence=({"kind": "auto_result", "value": "Full Auto completed"},),
        changed_paths=(),
        completed_at=datetime.now(UTC),
        receipt_sha256="a" * 64,
    )


@pytest.mark.asyncio
async def test_start_auto_dispatches_host_work_without_background_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden_job(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ChatGPT host Auto must not start a background job")

    def forbidden_runtime(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ChatGPT host Auto must not construct a nested runtime")

    monkeypatch.setattr("ouroboros.mcp.tools.auto_handler.start_background_tool_job", forbidden_job)
    monkeypatch.setattr(
        "ouroboros.mcp.tools.auto_handler.resolve_agent_runtime_backend", forbidden_runtime
    )
    context = _host_context(tmp_path)
    store = AutoStore(tmp_path / "auto-state")
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    interview = SimpleNamespace(host_dispatch_context=context)
    starter = StartAutoHandler(
        interview_handler=interview,
        store=store,
        event_store=event_store,
    )

    pending = await starter.handle({"goal": "Ship a safe release", "cwd": str(tmp_path)})

    assert pending.is_ok
    assert pending.value.meta["status"] == "host_work_pending"
    assert "job_id" not in pending.value.meta
    auto_session_id = pending.value.meta["auto_session_id"]
    order = pending.value.meta["work_order"]
    assert order["session_id"] == auto_session_id
    assert order["workspace_root"] == str(tmp_path.resolve())
    assert order["context"]["continuation"]["tool_name"] == "ouroboros_auto"
    assert "Do not invoke ouroboros_auto or ouroboros_start_auto recursively" in order["prompt"]

    auto = starter._inner_auto
    resumed = await auto.handle({"resume": auto_session_id})
    assert resumed.is_ok
    assert resumed.value.meta["work_order"]["dispatch_id"] == order["dispatch_id"]

    bridge = HostStageBridge(event_store, context)
    await bridge.complete(_receipt(order))
    continuation = order["context"]["continuation"]
    completed = await auto.handle(continuation["arguments"])
    assert completed.is_ok
    assert completed.value.meta["status"] == "completed"
    assert completed.value.meta["auto_session_id"] == auto_session_id
    assert store.load(auto_session_id).phase is AutoPhase.COMPLETE
    await event_store.close()


@pytest.mark.asyncio
async def test_direct_auto_uses_same_host_workspace_and_rejects_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    context = _host_context(workspace)
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'direct.db'}")
    auto = AutoHandler(
        interview_handler=SimpleNamespace(host_dispatch_context=context),
        store=AutoStore(tmp_path / "direct-state"),
        event_store=event_store,
    )

    pending = await auto.handle({"goal": "Build in place", "cwd": str(workspace)})
    escaped = await auto.handle({"goal": "Escape", "cwd": str(outside)})

    assert pending.is_ok
    assert pending.value.meta["status"] == "host_work_pending"
    assert pending.value.meta["work_order"]["workspace_root"] == str(workspace.resolve())
    assert escaped.is_err
    assert "active ChatGPT workspace" in str(escaped.error)
    await event_store.close()
