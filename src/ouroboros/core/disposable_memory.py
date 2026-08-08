"""Typed contracts for Disposable Memory result envelopes.

The caller-facing envelope deliberately has no artifact-body or transcript
field.  Large child output is reachable only through the explicit artifact
store API; the main session keeps this small, immutable projection.
"""

from __future__ import annotations

from enum import StrEnum
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ARTIFACT_REF_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DISPOSABLE_CONTRACT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_DISPOSABLE_ARTIFACT_BYTES = 1024 * 1024
MAX_DISPOSABLE_ENVELOPE_BYTES = 4 * 1024


class DisposableResultStatus(StrEnum):
    """Terminal status represented by a disposable result artifact."""

    COMPLETED = "completed"
    FAILED = "failed"


class DisposableResultSummary(BaseModel, frozen=True):
    """Small terminal summary carried inline with the artifact reference."""

    model_config = ConfigDict(extra="forbid")

    status: DisposableResultStatus


class DisposableResultEnvelope(BaseModel, frozen=True):
    """Bounded caller-facing projection for one disposable invocation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    contract_id: str = Field(min_length=1, max_length=128)
    artifact_ref: str
    result: DisposableResultSummary
    runtime_id: str = Field(min_length=1, max_length=200)
    duration_ms: int = Field(ge=0, le=2**63 - 1)
    events_emitted_count: int = Field(ge=0, le=2**63 - 1)

    @field_validator("contract_id")
    @classmethod
    def _path_safe_contract_id(cls, value: str) -> str:
        if not DISPOSABLE_CONTRACT_ID_PATTERN.fullmatch(value):
            raise ValueError("contract_id must be path-safe ASCII beginning with alphanumeric")
        return value

    @field_validator("artifact_ref")
    @classmethod
    def _valid_artifact_ref(cls, value: str) -> str:
        if not ARTIFACT_REF_PATTERN.fullmatch(value):
            raise ValueError("artifact_ref must use the sha256:<64 lowercase hex> form")
        return value


__all__ = [
    "ARTIFACT_REF_PATTERN",
    "DISPOSABLE_CONTRACT_ID_PATTERN",
    "MAX_DISPOSABLE_ARTIFACT_BYTES",
    "MAX_DISPOSABLE_ENVELOPE_BYTES",
    "DisposableResultEnvelope",
    "DisposableResultStatus",
    "DisposableResultSummary",
]
