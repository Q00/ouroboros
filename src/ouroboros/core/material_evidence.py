"""Opaque, non-authoritative references to locally retained material."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MaterialEvidenceRef(BaseModel):
    """A local lookup handle, never a factual or requirements assertion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    evidence_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: Literal["source_quote", "model_assumption"]
