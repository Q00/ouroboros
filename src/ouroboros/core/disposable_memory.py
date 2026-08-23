"""Typed contracts for Disposable Memory result envelopes.

The caller-facing envelope deliberately has no artifact-body or transcript
field.  Large child output is reachable only through the explicit artifact
store API; the main session keeps this small, immutable projection.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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

    # Version 2 drops `artifact_ref`, which version 1 required.  Removing a
    # required field is not an additive change, so it cannot happen under the
    # same number: this envelope is stamped into `artifact.referenced`, and an
    # append-only store would otherwise hold two shapes both claiming to be
    # version 1, with nothing able to tell which it was reading.  Rows written
    # before this keep saying 1 and keep their `artifact_ref`; a reader that
    # cares which shape it has is asking the field that exists to answer it.
    schema_version: Literal[2] = 2
    # Length is the whole rule.  The character restriction that used to live
    # here was a path-safe-filename rule, and a contract id stopped being this
    # store's filename when the store stopped being a directory.  It still
    # reaches one filename -- the AgentProcess cancel checkpoint -- which is
    # why that key derives from a digest of the id rather than from the id:
    # the restriction never made that mapping injective anyway, since `:` and
    # `_` both passed it and the checkpoint store folds the first onto the
    # second.
    contract_id: str = Field(min_length=1, max_length=128)
    result: DisposableResultSummary
    runtime_id: str = Field(min_length=1, max_length=200)
    duration_ms: int = Field(ge=0, le=2**63 - 1)
    events_emitted_count: int = Field(ge=0, le=2**63 - 1)


__all__ = [
    "MAX_DISPOSABLE_ARTIFACT_BYTES",
    "MAX_DISPOSABLE_ENVELOPE_BYTES",
    "DisposableResultEnvelope",
    "DisposableResultStatus",
    "DisposableResultSummary",
]
