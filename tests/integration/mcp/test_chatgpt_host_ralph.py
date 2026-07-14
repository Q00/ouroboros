"""ChatGPT-hosted Ralph stays in one Full lineage without nested jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ouroboros.mcp.tools.host_bridge import (
    HostCompletionReceipt,
    HostDispatchContext,
    HostStageBridge,
)
from ouroboros.mcp.tools.ralph_handlers import RalphHandler, StartRalphHandler
from ouroboros.persistence.event_store import EventStore


class _ForbiddenEvolve:
    async def handle(self, _arguments: dict[str, object]) -> None:
        raise AssertionError("host-driven Ralph must not invoke Evolve inside MCP")


def _context(workspace: Path) -> HostDispatchContext:
    return HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=workspace,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )


def _receipt(order: dict[str, object]) -> HostCompletionReceipt:
    criterion = str(order["acceptance_criteria"][0])  # type: ignore[index]
    return HostCompletionReceipt.model_validate(
        {
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
                    "criterion": criterion,
                    "passed": True,
                    "evidence_refs": ("ralph_loop:fixture",),
                },
            ),
            "evidence": (
                {
                    "kind": "ralph_loop",
                    "value": '{"stop_reason":"qa_passed","generations":2}',
                },
            ),
            "changed_paths": (),
            "completed_at": datetime.now(UTC).isoformat(),
            "receipt_sha256": "f" * 64,
        }
    )


@pytest.mark.asyncio
async def test_ralph_dispatches_and_resumes_same_host_lineage_without_nested_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden_background_job(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-driven Ralph must not enqueue a background job")

    async def forbidden_plugin_dispatch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-driven Ralph must not dispatch a nested plugin terminal")

    def forbidden_runner(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-driven Ralph must not construct RalphLoopRunner")

    monkeypatch.setattr(
        "ouroboros.mcp.tools.ralph_handlers.start_background_tool_job",
        forbidden_background_job,
    )
    monkeypatch.setattr(
        "ouroboros.mcp.tools.ralph_handlers.dispatch_plugin_terminal",
        forbidden_plugin_dispatch,
    )
    monkeypatch.setattr("ouroboros.mcp.tools.ralph_handlers.RalphLoopRunner", forbidden_runner)
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'ralph.db'}")
    context = _context(tmp_path)
    handler = RalphHandler(
        evolve_handler=_ForbiddenEvolve(),  # type: ignore[arg-type]
        event_store=store,
        host_dispatch_context=context,
    )
    arguments = {
        "lineage_id": "lineage-ralph",
        "seed_content": "goal: complete the product",
        "project_dir": str(tmp_path),
        "max_generations": 4,
    }

    pending = await handler.handle(arguments)

    assert pending.is_ok
    assert pending.value.meta["status"] == "host_work_pending"
    assert "job_id" not in pending.value.meta
    order = pending.value.meta["work_order"]
    assert order["session_id"] == "lineage-ralph"
    assert order["lineage_id"] == "lineage-ralph"
    assert order["workspace_root"] == str(tmp_path.resolve())
    continuation = order["context"]["continuation"]
    assert continuation["tool_name"] == "ouroboros_ralph"
    assert continuation["arguments"]["_host_dispatch_id"] == order["dispatch_id"]
    assert "Do not invoke ouroboros_ralph" in order["prompt"]

    repeated = await handler.handle(arguments)
    assert repeated.is_ok
    assert repeated.value.meta["work_order"]["dispatch_id"] == order["dispatch_id"]

    bridge = HostStageBridge(store, context)
    await bridge.complete(_receipt(order))
    completed = await handler.handle(continuation["arguments"])

    assert completed.is_ok
    assert completed.value.meta["status"] == "completed"
    assert completed.value.meta["dispatch_mode"] == "host_driven"
    assert completed.value.meta["dispatch_id"] == order["dispatch_id"]
    assert completed.value.meta["session_id"] == "lineage-ralph"
    assert completed.value.meta["lineage_id"] == "lineage-ralph"
    await store.close()


@pytest.mark.asyncio
async def test_start_ralph_alias_uses_same_host_contract_without_background_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden_background_job(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-driven StartRalph must not enqueue a background job")

    monkeypatch.setattr(
        "ouroboros.mcp.tools.ralph_handlers.start_background_tool_job",
        forbidden_background_job,
    )
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'start-ralph.db'}")
    context = _context(tmp_path)
    handler = StartRalphHandler(
        evolve_handler=_ForbiddenEvolve(),  # type: ignore[arg-type]
        event_store=store,
        host_dispatch_context=context,
    )

    pending = await handler.handle(
        {"lineage_id": "lineage-start-ralph", "project_dir": str(tmp_path)}
    )

    assert pending.is_ok
    assert pending.value.meta["status"] == "host_work_pending"
    assert "job_id" not in pending.value.meta
    order = pending.value.meta["work_order"]
    continuation = order["context"]["continuation"]
    assert continuation["tool_name"] == "ouroboros_start_ralph"
    await HostStageBridge(store, context).complete(_receipt(order))

    completed = await handler.handle(continuation["arguments"])

    assert completed.is_ok
    assert completed.value.meta["status"] == "completed"
    assert completed.value.meta["lineage_id"] == "lineage-start-ralph"
    await store.close()


@pytest.mark.asyncio
async def test_host_ralph_rejects_project_dir_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    handler = RalphHandler(
        evolve_handler=_ForbiddenEvolve(),  # type: ignore[arg-type]
        event_store=EventStore(f"sqlite+aiosqlite:///{tmp_path / 'boundary.db'}"),
        host_dispatch_context=_context(workspace),
    )

    result = await handler.handle({"lineage_id": "lineage-outside", "project_dir": str(outside)})

    assert result.is_err
    assert "active ChatGPT workspace" in str(result.error)
