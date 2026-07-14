"""Typed boundary between Ouroboros Full dispatches and a host task.

The types in this module describe work only.  They never launch a model,
agent process, or alternate workflow engine.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import json
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.event_store import EventStore


class HostTerminalStatus(StrEnum):
    """Terminal outcomes accepted from the host."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HostWorkOrder(BaseModel):
    """Immutable host work bound to one Full lineage and workspace policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    dispatch_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    lineage_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    workspace_root: Path
    sandbox_mode: str = Field(min_length=1)
    approval_policy: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    evidence_requirements: tuple[str, ...] = Field(min_length=1)
    created_at: datetime

    @field_validator("workspace_root")
    @classmethod
    def canonical_workspace(cls, value: Path) -> Path:
        """Require an existing directory and retain its canonical identity."""
        try:
            canonical = value.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("workspace_root must exist") from exc
        if not canonical.is_dir():
            raise ValueError("workspace_root must be a directory")
        return canonical

    @field_validator("created_at")
    @classmethod
    def require_created_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone offset")
        return value


class CriterionResult(BaseModel):
    """Evidence-backed outcome for one acceptance criterion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str = Field(min_length=1)
    passed: bool
    evidence_refs: tuple[str, ...] = ()


class HostCompletionReceipt(BaseModel):
    """Terminal receipt bound to the same workspace and host policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    dispatch_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    lineage_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    workspace_root: Path
    sandbox_mode: str = Field(min_length=1)
    approval_policy: str = Field(min_length=1)
    terminal_status: HostTerminalStatus
    criterion_results: tuple[CriterionResult, ...]
    evidence: tuple[dict[str, str], ...]
    changed_paths: tuple[Path, ...]
    completed_at: datetime
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure: dict[str, str] | None = None
    cancelled_at: datetime | None = None

    @field_validator("workspace_root")
    @classmethod
    def canonical_workspace(cls, value: Path) -> Path:
        """Canonicalize the receipt workspace using the work-order rule."""
        return HostWorkOrder.canonical_workspace(value)

    @field_validator("completed_at", "cancelled_at")
    @classmethod
    def require_terminal_timestamp_timezone(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("terminal timestamps must include a timezone offset")
        return value

    @model_validator(mode="after")
    def bind_changed_paths_to_workspace(self) -> HostCompletionReceipt:
        """Resolve changed paths and reject paths outside the selected workspace."""
        canonical_paths: list[Path] = []
        for changed_path in self.changed_paths:
            candidate = changed_path.expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace_root / candidate
            candidate = candidate.resolve(strict=False)
            if not candidate.is_relative_to(self.workspace_root):
                raise ValueError("changed_paths must remain inside workspace_root")
            canonical_paths.append(candidate)
        object.__setattr__(self, "changed_paths", tuple(canonical_paths))
        return self

    @model_validator(mode="after")
    def validate_terminal_fields(self) -> HostCompletionReceipt:
        """Require outcome-specific fields without weakening other terminals."""
        if self.terminal_status is HostTerminalStatus.FAILED:
            if not self.failure or not self.failure.get("code") or not self.failure.get("message"):
                raise ValueError("failure requires non-empty code and message")
        elif self.failure is not None:
            raise ValueError("failure is only valid for failed receipts")

        if self.terminal_status is HostTerminalStatus.CANCELLED:
            if self.cancelled_at is None:
                raise ValueError("cancelled_at is required for cancelled receipts")
            if self.cancelled_at > self.completed_at:
                raise ValueError("cancelled_at must not be after completed_at")
        elif self.cancelled_at is not None:
            raise ValueError("cancelled_at is only valid for cancelled receipts")
        return self


TERMINAL_EVENT: dict[HostTerminalStatus, str] = {
    HostTerminalStatus.COMPLETED: "execution.completed",
    HostTerminalStatus.FAILED: "host.dispatch.failed",
    HostTerminalStatus.CANCELLED: "host.dispatch.cancelled",
}


class HostBridgeError(ValueError):
    """Base error for rejected host bridge operations."""


class HostDispatchNotFound(HostBridgeError):
    """Raised when a receipt references an unknown dispatch."""


class HostDispatchConflict(HostBridgeError):
    """Raised when one dispatch ID is reused for different work."""


class HostDispatchIdentityError(HostBridgeError):
    """Raised when a receipt does not match its persisted work order."""


class HostReceiptConflict(HostBridgeError):
    """Raised when a terminal dispatch receives a different receipt."""


def _dispatch_event_id(dispatch_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"ouroboros-host-dispatch:{dispatch_id}"))


def _terminal_event_id(dispatch_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"ouroboros-host-terminal:{dispatch_id}"))


def dispatch_event_from_order(order: HostWorkOrder) -> BaseEvent:
    """Represent a typed host dispatch in the existing Full EventStore."""
    return BaseEvent(
        id=_dispatch_event_id(order.dispatch_id),
        type="host.dispatch.requested",
        timestamp=order.created_at,
        aggregate_type="host_dispatch",
        aggregate_id=order.lineage_id,
        data={"order": order.model_dump(mode="json"), "status": "requested"},
    )


def terminal_event_from_receipt(receipt: HostCompletionReceipt) -> BaseEvent:
    """Represent one deterministic terminal event for a host dispatch."""
    return BaseEvent(
        id=_terminal_event_id(receipt.dispatch_id),
        type=TERMINAL_EVENT[receipt.terminal_status],
        timestamp=receipt.completed_at,
        aggregate_type="host_dispatch",
        aggregate_id=receipt.lineage_id,
        data={
            "receipt": receipt.model_dump(mode="json"),
            "status": receipt.terminal_status.value,
        },
    )


def validate_receipt_identity(
    order: HostWorkOrder, receipt: HostCompletionReceipt
) -> None:
    """Reject any receipt that weakens or changes the original host boundary."""
    identity_fields = (
        "dispatch_id",
        "session_id",
        "lineage_id",
        "workspace_id",
        "workspace_root",
        "sandbox_mode",
        "approval_policy",
    )
    for field_name in identity_fields:
        expected = getattr(order, field_name)
        actual = getattr(receipt, field_name)
        if field_name == "workspace_root":
            actual = Path(actual).expanduser().resolve(strict=True)
        if actual != expected:
            raise HostDispatchIdentityError(
                f"receipt {field_name} does not match persisted work order"
            )


class HostBridgeHandler:
    """Persist and close typed host work in Ouroboros Full's EventStore."""

    def __init__(self, event_store: EventStore) -> None:
        self._event_store = event_store

    async def dispatch(self, order: HostWorkOrder) -> HostWorkOrder:
        event = dispatch_event_from_order(order)
        inserted = await self._event_store.append_idempotent(event)
        if inserted:
            return order
        stored = await self._event_store.require_event(event.id)
        stored_order = HostWorkOrder.model_validate(stored.data["order"])
        if stored_order != order:
            raise HostDispatchConflict(order.dispatch_id)
        return stored_order

    async def _require_order(self, dispatch_id: str) -> HostWorkOrder:
        try:
            stored = await self._event_store.require_event(_dispatch_event_id(dispatch_id))
        except PersistenceError as exc:
            raise HostDispatchNotFound(dispatch_id) from exc
        return HostWorkOrder.model_validate(stored.data["order"])

    async def complete(self, receipt: HostCompletionReceipt) -> HostCompletionReceipt:
        order = await self._require_order(receipt.dispatch_id)
        validate_receipt_identity(order, receipt)
        event = terminal_event_from_receipt(receipt)
        inserted = await self._event_store.append_idempotent(event)
        if inserted:
            return receipt
        stored = await self._event_store.require_event(event.id)
        stored_receipt = HostCompletionReceipt.model_validate(stored.data["receipt"])
        if stored_receipt != receipt:
            raise HostReceiptConflict(receipt.dispatch_id)
        return stored_receipt


class _HostReceiptToolHandler:
    """Shared MCP receipt parsing for completion and cancellation tools."""

    tool_name: str
    allowed_statuses: tuple[HostTerminalStatus, ...]

    def __init__(self, bridge: HostBridgeHandler) -> None:
        self._bridge = bridge

    async def handle(
        self, arguments: dict[str, Any]
    ) -> Result[MCPToolResult, MCPServerError]:
        try:
            receipt = HostCompletionReceipt.model_validate(arguments.get("receipt"))
            if receipt.terminal_status not in self.allowed_statuses:
                allowed = " or ".join(status.value for status in self.allowed_statuses)
                raise HostBridgeError(
                    f"{self.tool_name} requires terminal_status={allowed}"
                )
            stored = await self._bridge.complete(receipt)
        except (HostBridgeError, PersistenceError, ValueError) as exc:
            return Result.err(MCPToolError(str(exc), tool_name=self.tool_name))
        body = stored.model_dump(mode="json")
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=json.dumps(body, sort_keys=True, separators=(",", ":")),
                    ),
                ),
                meta={"receipt_sha256": stored.receipt_sha256},
            )
        )


class CompleteHostDispatchHandler(_HostReceiptToolHandler):
    """MCP tool that closes completed or failed Full host work."""

    tool_name = "ouroboros_complete_host_dispatch"
    allowed_statuses = (
        HostTerminalStatus.COMPLETED,
        HostTerminalStatus.FAILED,
    )

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name=self.tool_name,
            description=(
                "Submit evidence while closing an Ouroboros Full dispatch; "
                "this does not start a new workflow."
            ),
            parameters=(
                MCPToolParameter(
                    name="receipt",
                    type=ToolInputType.OBJECT,
                    description="Typed Full host completion or failure receipt.",
                    required=True,
                ),
            ),
        )


class CancelHostDispatchHandler(_HostReceiptToolHandler):
    """MCP tool that closes cancelled Full host work."""

    tool_name = "ouroboros_cancel_host_dispatch"
    allowed_statuses = (HostTerminalStatus.CANCELLED,)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name=self.tool_name,
            description=(
                "Submit cancellation evidence while closing an Ouroboros Full dispatch; "
                "this does not start a new workflow."
            ),
            parameters=(
                MCPToolParameter(
                    name="receipt",
                    type=ToolInputType.OBJECT,
                    description="Typed Full host cancellation receipt.",
                    required=True,
                ),
            ),
        )
