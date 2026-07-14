"""Typed boundary between Ouroboros Full dispatches and a host task.

The types in this module describe work only.  They never launch a model,
agent process, or alternate workflow engine.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HostTerminalStatus(StrEnum):
    """Terminal outcomes accepted from the host."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HostWorkOrder(BaseModel):
    """Immutable host work bound to one Full lineage and workspace policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0"
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


class CriterionResult(BaseModel):
    """Evidence-backed outcome for one acceptance criterion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str = Field(min_length=1)
    passed: bool
    evidence_refs: tuple[str, ...] = ()


class HostCompletionReceipt(BaseModel):
    """Terminal receipt bound to the same workspace and host policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "1.0"
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

    @field_validator("workspace_root")
    @classmethod
    def canonical_workspace(cls, value: Path) -> Path:
        """Canonicalize the receipt workspace using the work-order rule."""
        return HostWorkOrder.canonical_workspace(value)

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
