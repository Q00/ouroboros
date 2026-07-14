"""Contract tests for the ChatGPT host work-order boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
import pytest

from ouroboros.mcp.tools.host_bridge import (
    CriterionResult,
    HostCompletionReceipt,
    HostTerminalStatus,
    HostWorkOrder,
)
from ouroboros.mcp.tools.subagent import (
    build_host_work_order,
    build_subagent_payload,
)


def _order_data(workspace_root: Path) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "dispatch_id": "dispatch-01",
        "session_id": "session-01",
        "lineage_id": "lineage-01",
        "workspace_id": "workspace-01",
        "workspace_root": str(workspace_root),
        "sandbox_mode": "workspace-write",
        "approval_policy": "on-request",
        "prompt": "Create result.txt containing the word verified.",
        "context": {"seed_content": "goal: verify"},
        "acceptance_criteria": ["result.txt exists", "content equals verified"],
        "evidence_requirements": ["sha256", "changed_paths"],
        "created_at": "2026-07-14T05:00:00Z",
    }


def _receipt_data(workspace_root: Path) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "dispatch_id": "dispatch-01",
        "session_id": "session-01",
        "lineage_id": "lineage-01",
        "workspace_id": "workspace-01",
        "workspace_root": str(workspace_root),
        "sandbox_mode": "workspace-write",
        "approval_policy": "on-request",
        "terminal_status": "completed",
        "criterion_results": [
            {
                "criterion": "result.txt exists",
                "passed": True,
                "evidence_refs": ["sha256:result.txt"],
            }
        ],
        "evidence": [{"kind": "sha256", "value": "a" * 64}],
        "changed_paths": ["result.txt"],
        "completed_at": "2026-07-14T05:01:00Z",
        "receipt_sha256": "b" * 64,
    }


def test_work_order_binds_dispatch_to_workspace_and_policy(tmp_path: Path) -> None:
    order = HostWorkOrder.model_validate(_order_data(tmp_path))

    assert order.workspace_root == tmp_path.resolve()
    assert order.dispatch_id == "dispatch-01"
    assert order.sandbox_mode == "workspace-write"
    assert order.approval_policy == "on-request"


def test_work_order_rejects_missing_identifier(tmp_path: Path) -> None:
    data = _order_data(tmp_path)
    del data["lineage_id"]

    with pytest.raises(ValidationError, match="lineage_id"):
        HostWorkOrder.model_validate(data)


@pytest.mark.parametrize("schema_version", ["", "0.9", "2.0"])
def test_work_order_rejects_unsupported_schema_version(
    tmp_path: Path, schema_version: str
) -> None:
    data = _order_data(tmp_path)
    data["schema_version"] = schema_version

    with pytest.raises(ValidationError, match="schema_version"):
        HostWorkOrder.model_validate(data)


def test_work_order_rejects_naive_created_at(tmp_path: Path) -> None:
    data = _order_data(tmp_path)
    data["created_at"] = "2026-07-14T05:00:00"

    with pytest.raises(ValidationError, match="created_at"):
        HostWorkOrder.model_validate(data)


def test_work_order_rejects_nonexistent_workspace(tmp_path: Path) -> None:
    data = _order_data(tmp_path / "missing")

    with pytest.raises(ValidationError, match="workspace_root"):
        HostWorkOrder.model_validate(data)


def test_work_order_rejects_empty_acceptance_criteria(tmp_path: Path) -> None:
    data = _order_data(tmp_path)
    data["acceptance_criteria"] = []

    with pytest.raises(ValidationError, match="acceptance_criteria"):
        HostWorkOrder.model_validate(data)


def test_receipt_rejects_invalid_hash(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    data["receipt_sha256"] = "not-a-sha256"

    with pytest.raises(ValidationError, match="receipt_sha256"):
        HostCompletionReceipt.model_validate(data)


def test_receipt_rejects_missing_required_identity(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    del data["approval_policy"]

    with pytest.raises(ValidationError, match="approval_policy"):
        HostCompletionReceipt.model_validate(data)


@pytest.mark.parametrize("schema_version", ["", "0.9", "2.0"])
def test_receipt_rejects_unsupported_schema_version(
    tmp_path: Path, schema_version: str
) -> None:
    data = _receipt_data(tmp_path)
    data["schema_version"] = schema_version

    with pytest.raises(ValidationError, match="schema_version"):
        HostCompletionReceipt.model_validate(data)


def test_receipt_rejects_naive_completed_at(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    data["completed_at"] = "2026-07-14T05:01:00"

    with pytest.raises(ValidationError, match="completed_at"):
        HostCompletionReceipt.model_validate(data)


def test_receipt_resolves_changed_paths_inside_workspace(tmp_path: Path) -> None:
    receipt = HostCompletionReceipt.model_validate(_receipt_data(tmp_path))

    assert receipt.changed_paths == (tmp_path.resolve() / "result.txt",)
    assert receipt.terminal_status is HostTerminalStatus.COMPLETED
    assert isinstance(receipt.criterion_results[0], CriterionResult)


def test_receipt_rejects_attempted_path_escape(tmp_path: Path) -> None:
    data = _receipt_data(tmp_path)
    data["changed_paths"] = ["../outside.txt"]

    with pytest.raises(ValidationError, match="changed_paths"):
        HostCompletionReceipt.model_validate(data)


def test_subagent_conversion_preserves_prompt_and_context(tmp_path: Path) -> None:
    payload = build_subagent_payload(
        tool_name="ouroboros_execute_seed",
        title="Execute Full Seed",
        prompt="Run the approved Full Seed.",
        context={"seed_content": "goal: ship", "execute": True},
    )

    order = build_host_work_order(
        payload,
        dispatch_id="dispatch-02",
        session_id="session-02",
        lineage_id="lineage-02",
        workspace_id="workspace-02",
        workspace_root=tmp_path,
        sandbox_mode="workspace-write",
        approval_policy="on-request",
        acceptance_criteria=("Full execution completes",),
        evidence_requirements=("event_lineage",),
        created_at=datetime(2026, 7, 14, 5, 0, tzinfo=UTC),
    )

    assert order.prompt == payload.prompt
    assert order.context == payload.context
    assert order.dispatch_id == "dispatch-02"
    assert order.session_id == "session-02"
    assert order.lineage_id == "lineage-02"
