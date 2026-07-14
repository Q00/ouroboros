"""Host-driven Evaluate stays inside the active Ouroboros Full session."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler, StartEvaluateHandler
from ouroboros.mcp.tools.host_bridge import (
    HostBridgeHandler,
    HostCompletionReceipt,
    HostDispatchContext,
)
from ouroboros.persistence.event_store import EventStore


def _receipt(
    order: dict[str, object], *, failed_index: int | None = None
) -> HostCompletionReceipt:
    criteria = tuple(str(item) for item in order["acceptance_criteria"])
    return HostCompletionReceipt.model_validate(
        {
            "dispatch_id": order["dispatch_id"],
            "session_id": order["session_id"],
            "lineage_id": order["lineage_id"],
            "workspace_id": order["workspace_id"],
            "workspace_root": order["workspace_root"],
            "sandbox_mode": order["sandbox_mode"],
            "approval_policy": order["approval_policy"],
            "terminal_status": "failed" if failed_index is not None else "completed",
            "criterion_results": tuple(
                {
                    "criterion": criterion,
                    "passed": index != failed_index,
                    "evidence_refs": (f"host:{index}",),
                }
                for index, criterion in enumerate(criteria, start=1)
            ),
            "evidence": ({"kind": "evaluation", "value": "host:verdict"},),
            "changed_paths": (),
            "completed_at": datetime.now(UTC).isoformat(),
            "receipt_sha256": "e" * 64,
            "failure": (
                {
                    "code": "acceptance_criteria_not_met",
                    "message": "The evaluated artifact did not meet every criterion",
                }
                if failed_index is not None
                else None
            ),
        }
    )


@pytest.mark.asyncio
async def test_direct_evaluate_dispatches_and_continues_with_ordered_criteria(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-driven Evaluate must not construct an LLM adapter")

    monkeypatch.setattr("ouroboros.providers.create_llm_adapter", forbidden)
    monkeypatch.setattr("ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter", forbidden)
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evaluate.db'}")
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )
    handler = EvaluateHandler(event_store=store, host_dispatch_context=context)
    criteria = ["First criterion", "Second criterion", "Third criterion"]
    arguments = {
        "session_id": "session-evaluate",
        "artifact": "artifact under review",
        "acceptance_criteria": criteria,
        "working_dir": str(tmp_path),
    }

    pending = await handler.handle(arguments)

    assert pending.is_ok
    assert pending.value.meta["status"] == "host_work_pending"
    order = pending.value.meta["work_order"]
    assert order["session_id"] == "session-evaluate"
    assert order["lineage_id"] == "session-evaluate"
    assert order["acceptance_criteria"] == criteria

    await HostBridgeHandler(store).complete(_receipt(order))
    continuation = order["context"]["continuation"]
    resumed = await handler.handle(continuation["arguments"])

    assert resumed.is_ok
    assert resumed.value.meta["status"] == "completed"
    assert resumed.value.meta["session_id"] == "session-evaluate"
    assert [item["criterion"] for item in resumed.value.meta["criterion_results"]] == criteria
    await store.close()


@pytest.mark.asyncio
async def test_evaluate_continuation_returns_negative_verdict_in_same_order(
    tmp_path: Path,
) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'negative-evaluate.db'}")
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )
    handler = EvaluateHandler(event_store=store, host_dispatch_context=context)
    criteria = ["Criterion one", "Criterion two"]
    pending = await handler.handle(
        {
            "session_id": "session-negative-evaluate",
            "artifact": "artifact under review",
            "acceptance_criteria": criteria,
            "working_dir": str(tmp_path),
        }
    )
    order = pending.value.meta["work_order"]
    await HostBridgeHandler(store).complete(_receipt(order, failed_index=2))

    continuation = order["context"]["continuation"]
    resumed = await handler.handle(continuation["arguments"])

    assert resumed.is_ok
    assert resumed.value.meta["final_approved"] is False
    assert [item["criterion"] for item in resumed.value.meta["criterion_results"]] == criteria
    assert [item["passed"] for item in resumed.value.meta["criterion_results"]] == [True, False]
    await store.close()


@pytest.mark.asyncio
async def test_start_evaluate_returns_host_work_without_background_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden_background(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-driven StartEvaluate must not enqueue a job")

    def forbidden_adapter(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("host-driven StartEvaluate must not construct an LLM adapter")

    monkeypatch.setattr(
        "ouroboros.mcp.tools.evaluation_handlers.start_background_tool_job",
        forbidden_background,
    )
    monkeypatch.setattr(
        "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter", forbidden_adapter
    )
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'start-evaluate.db'}")
    context = HostDispatchContext(
        workspace_id="fixture-workspace",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        authority_source="fixture",
    )
    evaluate = EvaluateHandler(event_store=store, host_dispatch_context=context)
    handler = StartEvaluateHandler(evaluate_handler=evaluate, event_store=store)

    result = await handler.handle(
        {
            "session_id": "session-start-evaluate",
            "artifact": "artifact under review",
            "acceptance_criteria": ["Criterion A", "Criterion B"],
            "working_dir": str(tmp_path),
        }
    )

    assert result.is_ok
    assert result.value.meta["status"] == "host_work_pending"
    assert "job_id" not in result.value.meta
    assert result.value.meta["work_order"]["acceptance_criteria"] == [
        "Criterion A",
        "Criterion B",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_host_evaluate_rejects_working_dir_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    handler = EvaluateHandler(
        event_store=EventStore(f"sqlite+aiosqlite:///{tmp_path / 'boundary.db'}"),
        host_dispatch_context=HostDispatchContext(
            workspace_id="fixture-workspace",
            workspace_root=workspace,
            sandbox_mode="workspace-write",
            approval_policy="on-request",
            authority_source="fixture",
        ),
    )

    result = await handler.handle(
        {
            "session_id": "session-boundary",
            "artifact": "artifact under review",
            "working_dir": str(outside),
        }
    )

    assert result.is_err
    assert "active ChatGPT workspace" in str(result.error)
