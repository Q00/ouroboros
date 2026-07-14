"""Host-driven Evolve terminals never start a nested Full runtime."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler, StartEvolveStepHandler
from ouroboros.mcp.tools.host_bridge import (
    HostCompletionReceipt,
    HostDispatchContext,
    HostStageBridge,
)
from ouroboros.persistence.event_store import EventStore


class _ForbiddenEvolutionaryLoop:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"host-driven Evolve touched inline EvolutionaryLoop.{name}")


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
                    "evidence_refs": ("evolve_generation:1",),
                },
            ),
            "evidence": (
                {
                    "kind": "evolve_generation",
                    "value": '{"generation":1,"action":"continue","qa":"passed"}',
                },
            ),
            "changed_paths": (),
            "completed_at": datetime.now(UTC).isoformat(),
            "receipt_sha256": "e" * 64,
        }
    )


async def _complete(store: EventStore, context: HostDispatchContext, order: dict[str, object]) -> None:
    bridge = HostStageBridge(store, context)
    await bridge.complete(_receipt(order))


@pytest.mark.asyncio
async def test_direct_evolve_dispatches_and_resumes_same_host_lineage(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'direct-evolve.db'}")
    context = _context(tmp_path)
    handler = EvolveStepHandler(
        evolutionary_loop=_ForbiddenEvolutionaryLoop(),
        event_store=store,
        host_dispatch_context=context,
    )
    arguments = {
        "lineage_id": "lineage-direct",
        "seed_content": "goal: host evolve",
        "project_dir": str(tmp_path),
    }

    pending = await handler.handle(arguments)

    assert pending.is_ok
    assert pending.value.meta["status"] == "host_work_pending"
    order = pending.value.meta["work_order"]
    assert order["session_id"] == "lineage-direct"
    assert order["lineage_id"] == "lineage-direct"
    continuation = order["context"]["continuation"]
    assert continuation["tool_name"] == "ouroboros_evolve_step"
    await _complete(store, context, order)

    resumed = await handler.handle(continuation["arguments"])

    assert resumed.is_ok
    assert resumed.value.meta["status"] == "completed"
    assert resumed.value.meta["session_id"] == "lineage-direct"
    assert resumed.value.meta["lineage_id"] == "lineage-direct"
    assert resumed.value.meta["dispatch_id"] == order["dispatch_id"]
    await store.close()


@pytest.mark.asyncio
async def test_start_evolve_is_terminal_host_dispatch_without_background_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden_background_job(**_kwargs: object) -> None:
        raise AssertionError("host-driven StartEvolve must not enqueue a background job")

    monkeypatch.setattr(
        "ouroboros.mcp.tools.evolution_handlers.start_background_tool_job",
        forbidden_background_job,
    )
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'start-evolve.db'}")
    context = _context(tmp_path)
    inner = EvolveStepHandler(
        evolutionary_loop=_ForbiddenEvolutionaryLoop(),
        event_store=store,
        host_dispatch_context=context,
    )
    handler = StartEvolveStepHandler(
        evolve_handler=inner,
        event_store=store,
        host_dispatch_context=context,
    )

    pending = await handler.handle(
        {"lineage_id": "lineage-start", "project_dir": str(tmp_path), "execute": True}
    )

    assert pending.is_ok
    assert pending.value.meta["status"] == "host_work_pending"
    assert "job_id" not in pending.value.meta
    order = pending.value.meta["work_order"]
    continuation = order["context"]["continuation"]
    assert continuation["tool_name"] == "ouroboros_start_evolve_step"
    await _complete(store, context, order)

    resumed = await handler.handle(continuation["arguments"])

    assert resumed.is_ok
    assert resumed.value.meta["status"] == "completed"
    assert resumed.value.meta["lineage_id"] == "lineage-start"
    await store.close()


@pytest.mark.asyncio
async def test_host_evolve_rejects_project_dir_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    handler = EvolveStepHandler(
        evolutionary_loop=_ForbiddenEvolutionaryLoop(),
        event_store=EventStore(f"sqlite+aiosqlite:///{tmp_path / 'boundary.db'}"),
        host_dispatch_context=_context(workspace),
    )

    result = await handler.handle(
        {"lineage_id": "lineage-outside", "project_dir": str(outside)}
    )

    assert result.is_err
    assert "active ChatGPT workspace" in str(result.error)
